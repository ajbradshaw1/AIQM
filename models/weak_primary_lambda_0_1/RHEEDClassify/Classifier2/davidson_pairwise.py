"""Davidson probabilities for applicable three-way pair outcomes.

This module deliberately models only ``image1``, ``image2``, and ``tie``.
Reconstruction-specific ``not_apply`` remains an absolute-reward decision and
must be handled by the caller.

For scores ``s1`` and ``s2``, difference ``d = s1 - s2``, and global
log-propensity ``tau``, the registered Davidson logits are::

    [d / 2, -d / 2, tau]

Thus, conditional on a non-tie outcome, the image1 probability is
``sigmoid(d)``, matching the Bradley-Terry score scale.
"""

from __future__ import annotations

import math
from numbers import Real

import torch
from torch import nn
from torch.nn import functional as F


DEFAULT_LOG_TIE_PROPENSITY = 0.05
IMAGE1 = 0
IMAGE2 = 1
TIE = 2


def _validate_scores(
    score1: torch.Tensor,
    score2: torch.Tensor,
) -> None:
    if not isinstance(score1, torch.Tensor) or not isinstance(
        score2, torch.Tensor
    ):
        raise TypeError("Davidson scores must be torch tensors.")
    if score1.shape != score2.shape:
        raise ValueError("Davidson scores must have matching shapes.")
    if score1.device != score2.device or score1.dtype != score2.dtype:
        raise ValueError("Davidson scores must share device and dtype.")
    if not score1.is_floating_point():
        raise ValueError("Davidson scores must use a floating dtype.")
    if not torch.isfinite(score1).all() or not torch.isfinite(score2).all():
        raise ValueError("Davidson scores must be finite.")


def _global_log_propensity(
    value: torch.Tensor | Real,
    reference: torch.Tensor,
) -> torch.Tensor:
    if isinstance(value, torch.Tensor):
        if value.numel() != 1 or not value.is_floating_point():
            raise ValueError(
                "Global Davidson log-propensity must be one floating scalar."
            )
        if value.device != reference.device:
            raise ValueError(
                "Davidson log-propensity and scores must share a device."
            )
        output = value.to(dtype=reference.dtype).reshape(())
    elif isinstance(value, Real):
        if not math.isfinite(float(value)):
            raise ValueError("Davidson log-propensity must be finite.")
        output = reference.new_tensor(float(value))
    else:
        raise TypeError(
            "Davidson log-propensity must be a scalar tensor or real number."
        )
    if not torch.isfinite(output):
        raise ValueError("Davidson log-propensity must be finite.")
    return output


def davidson_logits(
    score1: torch.Tensor,
    score2: torch.Tensor,
    log_tie_propensity: torch.Tensor | Real,
) -> torch.Tensor:
    """Return ``[..., 3]`` Davidson logits for image1, image2, and tie."""

    _validate_scores(score1, score2)
    tau = _global_log_propensity(log_tie_propensity, score1)
    difference = score1 - score2
    return torch.stack(
        (
            0.5 * difference,
            -0.5 * difference,
            tau.expand_as(difference),
        ),
        dim=-1,
    )


def davidson_probabilities(
    score1: torch.Tensor,
    score2: torch.Tensor,
    log_tie_propensity: torch.Tensor | Real,
) -> torch.Tensor:
    """Return finite Davidson probabilities whose final dimension sums to one."""

    probabilities = F.softmax(
        davidson_logits(score1, score2, log_tie_propensity),
        dim=-1,
    )
    if not torch.isfinite(probabilities).all():
        raise FloatingPointError("Davidson probabilities are non-finite.")
    return probabilities


def effective_tie_margin(
    log_tie_propensity: torch.Tensor | Real,
    *,
    reference: torch.Tensor | None = None,
) -> torch.Tensor:
    """Return the signed deterministic argmax tie threshold ``2 * tau``.

    A negative value means the Davidson tie logit is below both win logits
    even when the scores are equal, so the tie decision region is empty.
    Callers must not clamp this value to zero and then apply an inclusive
    ``abs(difference) <= margin`` rule.
    """

    if reference is None:
        reference = (
            log_tie_propensity
            if isinstance(log_tie_propensity, torch.Tensor)
            else torch.empty((), dtype=torch.get_default_dtype())
        )
    elif not isinstance(reference, torch.Tensor) or not reference.is_floating_point():
        raise ValueError("Tie-margin reference must be a floating tensor.")
    tau = _global_log_propensity(log_tie_propensity, reference)
    return 2.0 * tau


def davidson_prediction(
    score1: torch.Tensor,
    score2: torch.Tensor,
    log_tie_propensity: torch.Tensor | Real,
) -> torch.Tensor:
    """Return class indices with explicit inclusive tie precedence.

    The tie class wins exactly when ``tau >= abs(score1 - score2) / 2``.
    Encoding this comparison explicitly avoids backend-dependent class-order
    behavior when logits are equal at the decision boundary.
    """

    _validate_scores(score1, score2)
    tau = _global_log_propensity(log_tie_propensity, score1)
    difference = score1 - score2
    decisive = torch.where(
        difference >= 0.0,
        torch.full_like(difference, IMAGE1, dtype=torch.long),
        torch.full_like(difference, IMAGE2, dtype=torch.long),
    )
    return torch.where(
        tau >= 0.5 * difference.abs(),
        torch.full_like(decisive, TIE),
        decisive,
    )


def davidson_negative_log_likelihood(
    score1: torch.Tensor,
    score2: torch.Tensor,
    target: torch.Tensor,
    log_tie_propensity: torch.Tensor | Real,
    *,
    weight: torch.Tensor | None = None,
    reduction: str = "mean",
) -> torch.Tensor:
    """Compute weighted NLL for targets 0, 1, or 2.

    A target of 3 is rejected intentionally: ``not_apply`` uses an absolute
    loss outside this module. With weights, ``mean`` divides by total weight,
    matching the repository's pair-row weighting convention.
    """

    logits = davidson_logits(score1, score2, log_tie_propensity)
    if not isinstance(target, torch.Tensor) or target.shape != score1.shape:
        raise ValueError("Davidson targets must match the score shape.")
    if target.device != score1.device or target.dtype != torch.long:
        raise ValueError("Davidson targets must be torch.long on score device.")
    if target.numel() == 0:
        raise ValueError("Davidson NLL requires at least one outcome.")
    if (target < IMAGE1).any() or (target > TIE).any():
        raise ValueError("Davidson targets are limited to image1/image2/tie.")
    if reduction not in {"none", "sum", "mean"}:
        raise ValueError(f"Unknown Davidson reduction: {reduction}.")

    per_row = F.cross_entropy(
        logits.reshape(-1, 3),
        target.reshape(-1),
        reduction="none",
    ).reshape_as(score1)
    denominator: torch.Tensor | None = None
    if weight is not None:
        if (
            not isinstance(weight, torch.Tensor)
            or weight.shape != score1.shape
            or weight.device != score1.device
            or not weight.is_floating_point()
            or not torch.isfinite(weight).all()
            or (weight < 0.0).any()
        ):
            raise ValueError(
                "Davidson weights must be aligned finite non-negative tensors."
            )
        weight = weight.to(dtype=per_row.dtype)
        per_row = per_row * weight
        denominator = weight.sum()
        if reduction == "mean" and denominator <= 0.0:
            raise ValueError("Davidson mean reduction requires positive weight.")

    if reduction == "none":
        output = per_row
    elif reduction == "sum":
        output = per_row.sum()
    elif denominator is None:
        output = per_row.mean()
    else:
        output = per_row.sum() / denominator
    if not torch.isfinite(output).all():
        raise FloatingPointError("Davidson negative log-likelihood is non-finite.")
    return output


class GlobalDavidsonPairwise(nn.Module):
    """A one-parameter Davidson module with global log tie propensity."""

    def __init__(
        self,
        initial_log_tie_propensity: float = DEFAULT_LOG_TIE_PROPENSITY,
        *,
        device: torch.device | str | None = None,
        dtype: torch.dtype | None = None,
    ) -> None:
        super().__init__()
        if not math.isfinite(float(initial_log_tie_propensity)):
            raise ValueError("Initial Davidson log-propensity must be finite.")
        self.log_tie_propensity = nn.Parameter(
            torch.tensor(
                float(initial_log_tie_propensity),
                device=device,
                dtype=dtype or torch.get_default_dtype(),
            )
        )

    def forward(
        self,
        score1: torch.Tensor,
        score2: torch.Tensor,
    ) -> torch.Tensor:
        return davidson_logits(score1, score2, self.log_tie_propensity)

    def probabilities(
        self,
        score1: torch.Tensor,
        score2: torch.Tensor,
    ) -> torch.Tensor:
        return davidson_probabilities(
            score1,
            score2,
            self.log_tie_propensity,
        )

    def predict(
        self,
        score1: torch.Tensor,
        score2: torch.Tensor,
    ) -> torch.Tensor:
        return davidson_prediction(
            score1,
            score2,
            self.log_tie_propensity,
        )

    def negative_log_likelihood(
        self,
        score1: torch.Tensor,
        score2: torch.Tensor,
        target: torch.Tensor,
        *,
        weight: torch.Tensor | None = None,
        reduction: str = "mean",
    ) -> torch.Tensor:
        return davidson_negative_log_likelihood(
            score1,
            score2,
            target,
            self.log_tie_propensity,
            weight=weight,
            reduction=reduction,
        )

    def extra_repr(self) -> str:
        return (
            "initial convention: logits=[d/2,-d/2,tau], "
            f"tau={float(self.log_tie_propensity.detach()):.6g}"
        )
