"""Weak-supervision four-class primary-reconstruction model.

This module is intentionally separate from ``primary_reconstruction_model``.
It consumes frozen 512-D image embeddings and predicts only the conditional
dominant superstructure among Twinned 2x1, c(6x2), RT13, and HTR.  The common
1x1 lattice is neither a class nor a negative label, and these probabilities
must not be interpreted as surface fractions.

Pairwise annotations provide two auxiliary signals.  Applicable
``image1/image2/tie`` rows train independent Davidson utilities, while the
applicability head learns a noisy-OR constraint saying that at least one
endpoint shows the named reconstruction.  A ``not_apply`` row trains only the
two endpoint applicability logits for that reconstruction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import torch
from torch import nn
from torch.nn import functional as F

try:
    from .davidson_pairwise import GlobalDavidsonPairwise
except ImportError:  # pragma: no cover - direct script/module use
    from davidson_pairwise import GlobalDavidsonPairwise


RECONSTRUCTION_CLASSES = (
    "twinned_2x1",
    "c_6x2",
    "rt13",
    "htr",
)
NUM_RECONSTRUCTIONS = len(RECONSTRUCTION_CLASSES)

IMAGE1 = 0
IMAGE2 = 1
TIE = 2
NOT_APPLY = 3

MODEL_FAMILY = "weak_four_class_primary_v1"
CHECKPOINT_KIND = "weak_primary_shadow_v1"
EXECUTION_SCOPE = "weak_shadow_only"
PAIRWISE_USED_METADATA_FIELD = "pairwise_used"
ALLOWED_PAIR_WEIGHTS = (0.0, 0.1, 0.3)


@dataclass(frozen=True)
class WeakPrimaryOutput:
    """Outputs of the three semantically independent heads."""

    primary_logits: torch.Tensor
    applicability_logits: torch.Tensor
    reward_utilities: torch.Tensor


@dataclass(frozen=True)
class PairwiseTargets:
    """One reconstruction-specific outcome per image pair.

    ``outcome`` uses ``0=image1``, ``1=image2``, ``2=tie``, and
    ``3=not_apply``.  An omitted ``row_weight`` means equal row weights.
    """

    reconstruction_index: torch.Tensor
    outcome: torch.Tensor
    row_weight: torch.Tensor | None = None


@dataclass(frozen=True)
class PairwiseLossBreakdown:
    rank: torch.Tensor
    applicability: torch.Tensor
    applicable_rows: int
    not_apply_rows: int


@dataclass(frozen=True)
class WeakPrimaryLossBreakdown:
    total: torch.Tensor
    primary: torch.Tensor
    pairwise_rank: torch.Tensor
    pairwise_applicability: torch.Tensor
    primary_rows: int
    applicable_pair_rows: int
    not_apply_pair_rows: int
    lambda_pair: float


@dataclass(frozen=True)
class ActionabilityPolicy:
    """Explicit, validation-derived thresholds for shadow inference.

    There is deliberately no implicit default policy.  Passing ``None`` to
    :func:`ensemble_inference` makes every result non-actionable.
    """

    min_predicted_applicability: float
    min_max_probability: float
    max_normalized_entropy: float
    max_seed_disagreement: float
    provenance: str

    def __post_init__(self) -> None:
        for name in (
            "min_predicted_applicability",
            "min_max_probability",
            "max_normalized_entropy",
            "max_seed_disagreement",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0,1].")
        if not self.provenance.strip():
            raise ValueError("Actionability policy provenance is required.")


class WeakPrimaryReconstructionModel(nn.Module):
    """``embedding -> shared MLP -> primary/applicability/reward``.

    The image encoder is deliberately absent: callers must supply embeddings
    from the registered, completely frozen DINOv2 encoder.
    """

    def __init__(
        self,
        embedding_dim: int = 512,
        shared_dim: int = 256,
        dropout: float = 0.1,
        initial_log_tie_propensity: float = 0.05,
    ) -> None:
        super().__init__()
        if embedding_dim < 1 or shared_dim < 1:
            raise ValueError("Model dimensions must be positive.")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0,1).")
        self.embedding_dim = int(embedding_dim)
        self.shared_dim = int(shared_dim)
        self.dropout = float(dropout)

        self.shared = nn.Sequential(
            nn.Linear(self.embedding_dim, self.shared_dim),
            nn.GELU(),
            nn.Dropout(self.dropout),
        )
        self.primary_head = nn.Linear(self.shared_dim, NUM_RECONSTRUCTIONS)
        self.applicability_head = nn.Linear(
            self.shared_dim, NUM_RECONSTRUCTIONS
        )
        self.pairwise_reward_head = nn.Linear(
            self.shared_dim, NUM_RECONSTRUCTIONS
        )
        self.davidson = GlobalDavidsonPairwise(
            initial_log_tie_propensity=initial_log_tie_propensity
        )

    def forward(self, embeddings: torch.Tensor) -> WeakPrimaryOutput:
        if (
            not isinstance(embeddings, torch.Tensor)
            or embeddings.ndim != 2
            or embeddings.shape[1] != self.embedding_dim
            or not embeddings.is_floating_point()
            or not torch.isfinite(embeddings).all()
        ):
            raise ValueError(
                "embeddings must be finite floating tensors with shape "
                f"[batch,{self.embedding_dim}]."
            )
        shared = self.shared(embeddings)
        output = WeakPrimaryOutput(
            primary_logits=self.primary_head(shared),
            applicability_logits=self.applicability_head(shared),
            reward_utilities=self.pairwise_reward_head(shared),
        )
        _validate_output(output)
        return output

    def predict_proba(
        self,
        embeddings: torch.Tensor,
        *,
        temperature: float = 1.0,
    ) -> torch.Tensor:
        return conditional_primary_probabilities(
            self.forward(embeddings), temperature=temperature
        )


def conditional_primary_probabilities(
    output: WeakPrimaryOutput,
    *,
    temperature: float = 1.0,
) -> torch.Tensor:
    """Return four conditional probabilities that sum to one per frame."""

    _validate_output(output)
    value = float(temperature)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("temperature must be positive and finite.")
    probabilities = F.softmax(output.primary_logits / value, dim=1)
    if (
        not torch.isfinite(probabilities).all()
        or not torch.allclose(
            probabilities.sum(dim=1),
            torch.ones(
                len(probabilities),
                device=probabilities.device,
                dtype=probabilities.dtype,
            ),
            atol=1e-6,
            rtol=1e-6,
        )
    ):
        raise FloatingPointError("Conditional probabilities are invalid.")
    return probabilities


def primary_classification_loss(
    output: WeakPrimaryOutput,
    targets: torch.Tensor,
    *,
    label_smoothing: float = 0.05,
    sample_weight: torch.Tensor | None = None,
) -> torch.Tensor:
    """Four-class cross entropy for the superstructure anchors only."""

    _validate_output(output)
    rows = len(output.primary_logits)
    _validate_class_targets(targets, rows=rows, name="primary targets")
    if targets.device != output.primary_logits.device:
        raise ValueError("Primary targets and model output must share a device.")
    smoothing = float(label_smoothing)
    if not math.isfinite(smoothing) or not 0.0 <= smoothing < 1.0:
        raise ValueError("label_smoothing must be finite and in [0,1).")
    if rows == 0:
        return output.primary_logits.sum() * 0.0
    per_row = F.cross_entropy(
        output.primary_logits,
        targets,
        label_smoothing=smoothing,
        reduction="none",
    )
    return _weighted_mean(per_row, sample_weight)


def pairwise_auxiliary_loss(
    model: WeakPrimaryReconstructionModel,
    first_output: WeakPrimaryOutput,
    second_output: WeakPrimaryOutput,
    targets: PairwiseTargets,
) -> PairwiseLossBreakdown:
    """Davidson ranking plus per-type endpoint applicability losses."""

    if not isinstance(model, WeakPrimaryReconstructionModel):
        raise TypeError("model must be WeakPrimaryReconstructionModel.")
    _validate_output(first_output)
    _validate_output(second_output)
    rows = len(first_output.primary_logits)
    if len(second_output.primary_logits) != rows:
        raise ValueError("Pair endpoint outputs must have equal row counts.")
    if (
        second_output.primary_logits.device
        != first_output.primary_logits.device
        or second_output.primary_logits.dtype
        != first_output.primary_logits.dtype
    ):
        raise ValueError("Pair endpoint outputs must share device and dtype.")
    reconstruction, outcome, weight = _validate_pair_targets(
        targets, rows=rows, device=first_output.primary_logits.device
    )
    if rows == 0:
        zero = first_output.primary_logits.sum() * 0.0
        return PairwiseLossBreakdown(zero, zero, 0, 0)

    row_index = torch.arange(rows, device=reconstruction.device)
    first_reward = first_output.reward_utilities[row_index, reconstruction]
    second_reward = second_output.reward_utilities[row_index, reconstruction]
    first_app = first_output.applicability_logits[row_index, reconstruction]
    second_app = second_output.applicability_logits[row_index, reconstruction]

    applicable = outcome < NOT_APPLY
    not_apply = ~applicable
    if applicable.any():
        selected_weight = None if weight is None else weight[applicable]
        rank_loss = model.davidson.negative_log_likelihood(
            first_reward[applicable],
            second_reward[applicable],
            outcome[applicable],
            weight=selected_weight,
        )
    else:
        rank_loss = (first_reward.sum() + second_reward.sum()) * 0.0

    per_row_app = torch.empty_like(first_app)
    if applicable.any():
        # p(at least one) has logit log(exp(a)+exp(b)+exp(a+b)).  This is
        # exactly the independent noisy-OR, expressed without cancellation.
        noisy_or_logit = torch.logsumexp(
            torch.stack(
                (
                    first_app[applicable],
                    second_app[applicable],
                    first_app[applicable] + second_app[applicable],
                ),
                dim=1,
            ),
            dim=1,
        )
        per_row_app[applicable] = F.softplus(-noisy_or_logit)
    if not_apply.any():
        # ``not_apply`` says neither endpoint shows this type.  It does not
        # train the Davidson utilities or any other reconstruction column.
        per_row_app[not_apply] = 0.5 * (
            F.softplus(first_app[not_apply])
            + F.softplus(second_app[not_apply])
        )
    applicability_loss = _weighted_mean(per_row_app, weight)
    if not torch.isfinite(rank_loss) or not torch.isfinite(applicability_loss):
        raise FloatingPointError("Pairwise auxiliary loss is non-finite.")
    return PairwiseLossBreakdown(
        rank=rank_loss,
        applicability=applicability_loss,
        applicable_rows=int(applicable.sum()),
        not_apply_rows=int(not_apply.sum()),
    )


def weak_primary_loss(
    model: WeakPrimaryReconstructionModel,
    anchor_output: WeakPrimaryOutput | None,
    anchor_targets: torch.Tensor | None,
    *,
    pair_first_output: WeakPrimaryOutput | None = None,
    pair_second_output: WeakPrimaryOutput | None = None,
    pair_targets: PairwiseTargets | None = None,
    lambda_pair: float,
    label_smoothing: float = 0.05,
    primary_sample_weight: torch.Tensor | None = None,
) -> WeakPrimaryLossBreakdown:
    """Compose anchor classification and optional pairwise auxiliary loss."""

    pair_weight = _validate_pair_weight(lambda_pair)
    if (anchor_output is None) != (anchor_targets is None):
        raise ValueError("anchor_output and anchor_targets must appear together.")
    if anchor_output is None:
        reference = _pair_reference(pair_first_output, pair_second_output)
        primary = reference.sum() * 0.0
        primary_rows = 0
    else:
        primary = primary_classification_loss(
            anchor_output,
            anchor_targets,
            label_smoothing=label_smoothing,
            sample_weight=primary_sample_weight,
        )
        primary_rows = len(anchor_targets)

    pair_items = (pair_first_output, pair_second_output, pair_targets)
    if any(item is not None for item in pair_items):
        if not all(item is not None for item in pair_items):
            raise ValueError("Both pair outputs and pair_targets are required.")
        pair = pairwise_auxiliary_loss(
            model,
            pair_first_output,
            pair_second_output,
            pair_targets,
        )
    else:
        if pair_weight > 0.0:
            raise ValueError("Positive lambda_pair requires pairwise rows.")
        zero = primary * 0.0
        pair = PairwiseLossBreakdown(zero, zero, 0, 0)

    total = primary + pair_weight * (pair.rank + pair.applicability)
    if not torch.isfinite(total):
        raise FloatingPointError("Weak primary loss is non-finite.")
    return WeakPrimaryLossBreakdown(
        total=total,
        primary=primary,
        pairwise_rank=pair.rank,
        pairwise_applicability=pair.applicability,
        primary_rows=primary_rows,
        applicable_pair_rows=pair.applicable_rows,
        not_apply_pair_rows=pair.not_apply_rows,
        lambda_pair=pair_weight,
    )


def weak_model_metadata(
    model: WeakPrimaryReconstructionModel,
    *,
    lambda_pair: float,
) -> dict[str, object]:
    """Return the non-deployable semantic contract for a checkpoint."""

    value = _validate_pair_weight(lambda_pair)
    if not isinstance(model, WeakPrimaryReconstructionModel):
        raise TypeError("model must be WeakPrimaryReconstructionModel.")
    return {
        "model_family": MODEL_FAMILY,
        "checkpoint_kind": CHECKPOINT_KIND,
        "execution_scope": EXECUTION_SCOPE,
        "deployment_authorized": False,
        "autonomous_control_authorized": False,
        PAIRWISE_USED_METADATA_FIELD: value > 0.0,
        "lambda_pair": value,
        "embedding_dim": model.embedding_dim,
        "shared_dim": model.shared_dim,
        "dropout": model.dropout,
        "primary_classes": list(RECONSTRUCTION_CLASSES),
        "one_by_one_is_class": False,
        "output_semantics": (
            "conditional_dominant_superstructure_probability_not_surface_fraction"
        ),
        "actionability_policy_required": True,
    }


@torch.no_grad()
def ensemble_inference(
    models: Sequence[WeakPrimaryReconstructionModel],
    embeddings: torch.Tensor,
    *,
    policy: ActionabilityPolicy | None,
    external_abstain_reasons: Sequence[str | None] | None = None,
) -> list[dict[str, object]]:
    """Return the exact shadow inference contract for a seed ensemble.

    ``seed_disagreement`` is normalized Jensen-Shannon divergence among seed
    probability vectors.  It is zero for a one-model ensemble and lies in
    ``[0,1]``.  Missing thresholds fail closed instead of borrowing an
    unregistered default.
    """

    if not models:
        raise ValueError("At least one seed model is required.")
    if policy is not None and not isinstance(policy, ActionabilityPolicy):
        raise TypeError("policy must be ActionabilityPolicy or None.")
    rows = len(embeddings)
    if external_abstain_reasons is None:
        reasons: Sequence[str | None] = (None,) * rows
    else:
        if len(external_abstain_reasons) != rows:
            raise ValueError("External abstain reasons must be row-aligned.")
        reasons = external_abstain_reasons

    probability_by_seed: list[torch.Tensor] = []
    applicability_by_seed: list[torch.Tensor] = []
    modes = [model.training for model in models]
    try:
        for model in models:
            model.eval()
            output = model(embeddings)
            probability_by_seed.append(conditional_primary_probabilities(output))
            applicability_by_seed.append(torch.sigmoid(output.applicability_logits))
    finally:
        for model, training in zip(models, modes):
            model.train(training)

    probabilities = torch.stack(probability_by_seed, dim=0)
    applicability = torch.stack(applicability_by_seed, dim=0).mean(dim=0)
    mean_probability = probabilities.mean(dim=0)
    entropy = -(
        mean_probability * mean_probability.clamp_min(1e-12).log()
    ).sum(dim=1)
    per_seed_entropy = -(
        probabilities * probabilities.clamp_min(1e-12).log()
    ).sum(dim=2)
    disagreement = (entropy - per_seed_entropy.mean(dim=0)) / math.log(
        NUM_RECONSTRUCTIONS
    )
    disagreement = disagreement.clamp(0.0, 1.0)
    normalized_entropy = entropy / math.log(NUM_RECONSTRUCTIONS)
    prediction = mean_probability.argmax(dim=1)

    results: list[dict[str, object]] = []
    for row in range(rows):
        predicted = int(prediction[row])
        maximum = float(mean_probability[row, predicted])
        evidence = float(applicability[row, predicted])
        row_entropy = float(entropy[row])
        row_normalized_entropy = float(normalized_entropy[row])
        row_disagreement = float(disagreement[row])

        reason = reasons[row]
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError("External abstain reasons must be non-empty strings.")
        if reason is None and policy is None:
            reason = "actionability_policy_missing"
        elif reason is None and evidence < policy.min_predicted_applicability:
            reason = "insufficient_superstructure_evidence"
        elif reason is None and maximum < policy.min_max_probability:
            reason = "low_primary_confidence"
        elif reason is None and row_normalized_entropy > policy.max_normalized_entropy:
            reason = "high_primary_entropy"
        elif reason is None and row_disagreement > policy.max_seed_disagreement:
            reason = "high_seed_disagreement"

        results.append(
            {
                "conditional_primary_probabilities": {
                    name: float(mean_probability[row, index])
                    for index, name in enumerate(RECONSTRUCTION_CLASSES)
                },
                "actionable": reason is None,
                "abstain_reason": reason,
                "entropy": row_entropy,
                "seed_disagreement": row_disagreement,
            }
        )
    return results


def _validate_output(output: WeakPrimaryOutput) -> None:
    if not isinstance(output, WeakPrimaryOutput):
        raise TypeError("output must be WeakPrimaryOutput.")
    if output.primary_logits.ndim != 2:
        raise ValueError("Primary logits must be two-dimensional.")
    rows = len(output.primary_logits)
    expected = (rows, NUM_RECONSTRUCTIONS)
    tensors = (
        output.primary_logits,
        output.applicability_logits,
        output.reward_utilities,
    )
    if any(
        tensor.shape != expected
        or not tensor.is_floating_point()
        or tensor.device != output.primary_logits.device
        or tensor.dtype != output.primary_logits.dtype
        or not torch.isfinite(tensor).all()
        for tensor in tensors
    ):
        raise ValueError("Weak primary outputs have invalid shapes or values.")


def _validate_class_targets(
    targets: torch.Tensor,
    *,
    rows: int,
    name: str,
) -> None:
    if (
        not isinstance(targets, torch.Tensor)
        or targets.shape != (rows,)
        or targets.dtype != torch.long
        or (rows and ((targets < 0).any() or (targets >= NUM_RECONSTRUCTIONS).any()))
    ):
        raise ValueError(
            f"{name} must be torch.long with values 0..{NUM_RECONSTRUCTIONS - 1}."
        )


def _validate_pair_targets(
    targets: PairwiseTargets,
    *,
    rows: int,
    device: torch.device,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor | None]:
    if not isinstance(targets, PairwiseTargets):
        raise TypeError("targets must be PairwiseTargets.")
    reconstruction = targets.reconstruction_index
    outcome = targets.outcome
    _validate_class_targets(
        reconstruction, rows=rows, name="reconstruction_index"
    )
    if (
        not isinstance(outcome, torch.Tensor)
        or outcome.shape != (rows,)
        or outcome.dtype != torch.long
        or (rows and ((outcome < IMAGE1).any() or (outcome > NOT_APPLY).any()))
    ):
        raise ValueError("Pair outcomes must be torch.long values 0..3.")
    if reconstruction.device != device or outcome.device != device:
        raise ValueError("Pair targets and model outputs must share a device.")
    weight = targets.row_weight
    if weight is not None:
        if (
            not isinstance(weight, torch.Tensor)
            or weight.shape != (rows,)
            or weight.device != device
            or not weight.is_floating_point()
            or not torch.isfinite(weight).all()
            or (weight < 0.0).any()
            or (rows and float(weight.sum()) <= 0.0)
        ):
            raise ValueError("row_weight must be finite, non-negative, and nonzero.")
    return reconstruction, outcome, weight


def _weighted_mean(
    values: torch.Tensor,
    weights: torch.Tensor | None,
) -> torch.Tensor:
    if len(values) == 0:
        return values.sum() * 0.0
    if weights is None:
        return values.mean()
    if (
        weights.shape != values.shape
        or weights.device != values.device
        or not weights.is_floating_point()
        or not torch.isfinite(weights).all()
        or (weights < 0.0).any()
        or float(weights.sum()) <= 0.0
    ):
        raise ValueError("Weights must be aligned finite non-negative values.")
    weights = weights.to(dtype=values.dtype)
    return (values * weights).sum() / weights.sum()


def _validate_pair_weight(value: float) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or not any(
        math.isclose(numeric, allowed, rel_tol=0.0, abs_tol=1e-12)
        for allowed in ALLOWED_PAIR_WEIGHTS
    ):
        raise ValueError(f"lambda_pair must be one of {ALLOWED_PAIR_WEIGHTS}.")
    return numeric


def _pair_reference(
    first: WeakPrimaryOutput | None,
    second: WeakPrimaryOutput | None,
) -> torch.Tensor:
    if first is None or second is None:
        raise ValueError("At least anchor or complete pair outputs are required.")
    _validate_output(first)
    _validate_output(second)
    return first.primary_logits
