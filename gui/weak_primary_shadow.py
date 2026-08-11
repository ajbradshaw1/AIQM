"""Runtime bridge for the brightness-robust four-output shadow ensemble.

This route is deliberately isolated from the deployed classifier.  Its four
outputs are conditional probabilities among visible superstructures; there is
no 1x1/none class and no independently validated presence gate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np


LAMBDA_PAIR = 0.1
BUNDLE_FAMILY = "brightness_robust_weak_primary_v1"
BRIGHTNESS_POLICY = "all_extreme"
SOURCE_BUNDLE_NAME = "four_output_no_1x1_all_extreme"
EXPECTED_FOLDS = frozenset(range(4))
EXPECTED_SEEDS = frozenset((17, 29, 43))
EXPECTED_CHECKPOINT_COUNT = 36
CHECKPOINT_RE = re.compile(
    r"fold_(?P<fold>\d+)__pair_(?P<pair>.+)__seed_(?P<seed>\d+)"
    r"__lambda_0\.1$"
)
MODEL_CLASSES = ("twinned_2x1", "c_6x2", "rt13", "htr")
DISPLAY_CLASSES = (
    "Twinned (2x1)", "c(6x2)", "rt13xrt13", "HTR",
)
EXECUTION_SCOPE = "weak_shadow_only"
DEFAULT_MIN_AVAILABLE_MEMORY_MB = 1536


def _available_system_memory_mb() -> Optional[float]:
    """Return Windows available physical memory without another dependency."""
    if sys.platform != "win32":
        return None
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.ullAvailPhys / (1024 * 1024)


def _require_available_memory() -> None:
    configured = os.environ.get("AIQM_WEAK_PRIMARY_MIN_AVAILABLE_MB", "1536")
    try:
        required_mb = max(0.0, float(configured))
    except ValueError as error:
        raise ValueError(
            "AIQM_WEAK_PRIMARY_MIN_AVAILABLE_MB must be numeric."
        ) from error
    available_mb = _available_system_memory_mb()
    if available_mb is not None and available_mb < required_mb:
        raise MemoryError(
            "Weak-primary shadow was not loaded: "
            f"{available_mb:.0f} MB RAM available, {required_mb:.0f} MB required."
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classifier2_dir(ai_repo_root: str | Path) -> Path:
    root = Path(ai_repo_root).expanduser().resolve()
    candidates = (
        root / "src" / "classifiers" / "classifier2",
        root / "Classifier2",
    )
    return next((path for path in candidates if path.is_dir()), candidates[1])


def default_artifact_root(ai_repo_root: str | Path) -> Path:
    root = Path(ai_repo_root).expanduser().resolve()
    return root / "brightness_robust_four_output_all_extreme"


def _resolve_release_file(artifact_root: Path, record: dict) -> Path:
    """Resolve a release record while preserving the upstream manifest.

    The research manifest stores repository-relative source paths.  The GUI
    package uses a shorter Windows-safe directory, so the suffix following the
    bundle name is resolved inside ``artifact_root``.
    """
    source_path = Path(str(record.get("path", "")))
    try:
        suffix = source_path.parts[
            source_path.parts.index(SOURCE_BUNDLE_NAME) + 1:
        ]
    except ValueError as error:
        raise ValueError(
            f"Release path is outside bundle {SOURCE_BUNDLE_NAME}: {source_path}"
        ) from error
    path = artifact_root.joinpath(*suffix).resolve()
    try:
        path.relative_to(artifact_root.resolve())
    except ValueError as error:
        raise ValueError(f"Release path escapes bundle: {source_path}") from error
    if not path.is_file():
        raise FileNotFoundError(f"Release file is missing: {path}")
    expected_bytes = int(record.get("bytes", -1))
    if (
        path.stat().st_size != expected_bytes
        or _sha256_file(path) != record.get("sha256")
    ):
        raise ValueError(f"Release integrity validation failed: {path}")
    return path


def load_release_manifest(artifact_root: Path) -> tuple[dict, list[Path]]:
    """Validate the four-output, non-deployable release and all file hashes."""
    manifest_path = artifact_root / "MANIFEST.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Release manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contract = manifest.get("contracts", {}).get("four_output", {})
    cells = manifest.get("cells", [])
    if (
        manifest.get("schema_version") != 1
        or manifest.get("bundle_family") != BUNDLE_FAMILY
        or manifest.get("brightness_policy") != BRIGHTNESS_POLICY
        or not math.isclose(float(manifest.get("lambda_pair", -1)), LAMBDA_PAIR)
        or manifest.get("execution_scope") != EXECUTION_SCOPE
        or manifest.get("deployment_eligible") is not False
        or manifest.get("ensemble_cells") != EXPECTED_CHECKPOINT_COUNT
        or tuple(contract.get("classes", ())) != MODEL_CLASSES
        or contract.get("semantics")
        != "conditional_primary_type_probability_not_surface_fraction"
        or "five_output" in manifest.get("contracts", {})
        or len(cells) != EXPECTED_CHECKPOINT_COUNT
    ):
        raise ValueError("Release manifest violates the four-output shadow contract.")
    encoder_path = _resolve_release_file(artifact_root, manifest["encoder"])
    metadata_path = _resolve_release_file(
        artifact_root, manifest["encoder_metadata"],
    )
    checkpoint_paths = []
    seen_cells = set()
    for cell in cells:
        name = str(cell.get("cell", ""))
        if name in seen_cells or CHECKPOINT_RE.fullmatch(name) is None:
            raise ValueError(f"Invalid or duplicate release cell: {name}")
        seen_cells.add(name)
        path = _resolve_release_file(artifact_root, cell["conditional_head"])
        if path.parent.name != name:
            raise ValueError(f"Release cell/path mismatch: {name}")
        checkpoint_paths.append(path)
    discover_lambda_point_one_checkpoints(
        artifact_root / "conditional_four_output_heads"
    )
    manifest["_resolved_encoder_path"] = encoder_path
    manifest["_resolved_encoder_metadata_path"] = metadata_path
    return manifest, checkpoint_paths


def discover_lambda_point_one_checkpoints(checkpoint_root: Path) -> list[Path]:
    """Return the complete 4-fold × 3-run × 3-seed collection or fail."""
    paths = sorted(checkpoint_root.glob("*__lambda_0.1/model.pth"))
    identities: set[tuple[int, str, int]] = set()
    pair_runs: set[str] = set()
    for path in paths:
        match = CHECKPOINT_RE.fullmatch(path.parent.name)
        if match is None:
            raise ValueError(f"Unexpected λ=0.1 checkpoint directory: {path.parent.name}")
        identity = (
            int(match.group("fold")), match.group("pair"),
            int(match.group("seed")),
        )
        if identity in identities:
            raise ValueError(f"Duplicate λ=0.1 checkpoint identity: {identity}")
        identities.add(identity)
        pair_runs.add(identity[1])
    expected = {
        (fold, pair, seed)
        for fold in EXPECTED_FOLDS
        for pair in pair_runs
        for seed in EXPECTED_SEEDS
    }
    if (
        len(paths) != EXPECTED_CHECKPOINT_COUNT
        or len(pair_runs) != 3
        or identities != expected
    ):
        raise ValueError(
            "λ=0.1 collection must contain exactly 36 cells "
            "(4 folds × 3 pair runs × 3 seeds)."
        )
    return paths


class WeakPrimaryShadowBridge:
    """Load and evaluate the brightness-robust non-deployable collection."""

    input_mode = "single_frame_weak_primary_shadow"
    execution_scope = EXECUTION_SCOPE
    deployment_eligible = False

    def __init__(
        self,
        ai_repo_root: str | Path,
        artifact_root: Optional[str | Path] = None,
        device: Optional[str] = None,
        *,
        _encoder_factory: Optional[Callable] = None,
        _model_factory: Optional[Callable] = None,
    ) -> None:
        import torch

        _require_available_memory()
        self._torch = torch
        self._c2_dir = _classifier2_dir(ai_repo_root)
        self._repo = self._c2_dir.parent
        self.artifact_root = Path(
            artifact_root or default_artifact_root(ai_repo_root)
        ).expanduser().resolve()
        manifest, self.checkpoint_paths = load_release_manifest(
            self.artifact_root
        )
        self.encoder_path = manifest.pop("_resolved_encoder_path")
        self.encoder_metadata_path = manifest.pop("_resolved_encoder_metadata_path")
        self.manifest = manifest
        self.manifest_path = self.artifact_root / "MANIFEST.json"
        metadata = json.loads(self.encoder_metadata_path.read_text(encoding="utf-8"))
        observed_encoder_sha = _sha256_file(self.encoder_path)
        if (
            metadata.get("backbone")
            != "dinov2_vits14_pretrained_zeropad512"
            or metadata.get("feature_projection") != "zero_pad_384_to_512"
            or metadata.get("local_data_fit") is not False
            or metadata.get("train_frames") != 0
            or metadata.get("checkpoint_sha256") != observed_encoder_sha
        ):
            raise ValueError("DINOv2 encoder provenance validation failed.")

        requested = device or "auto"
        if requested == "auto":
            requested = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(requested)

        for path in (self._repo, self._c2_dir):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        if _encoder_factory is None:
            from backbones import build_encoder
            _encoder_factory = lambda: build_encoder(
                "dinov2_vits14_pretrained_zeropad512",
                imagenet_init=False,
            )[0]
        if _model_factory is None:
            from weak_primary_model import WeakPrimaryReconstructionModel
            _model_factory = WeakPrimaryReconstructionModel

        encoder = _encoder_factory()
        encoder_state = torch.load(
            self.encoder_path, map_location="cpu", weights_only=True,
        )
        encoder.load_state_dict(encoder_state, strict=True)
        self._encoder = encoder.requires_grad_(False).eval().to(self.device)

        self._models = []
        self._temperatures: list[float] = []
        checkpoint_hashes: list[str] = []
        for cell, path in zip(self.manifest["cells"], self.checkpoint_paths):
            payload = torch.load(path, map_location="cpu", weights_only=True)
            self._validate_checkpoint(
                payload, path, cell["cell"], observed_encoder_sha,
            )
            state = payload["model_state_dict"]
            shared_dim = int(state["shared.0.weight"].shape[0])
            dropout = float(payload.get("optimizer", {}).get("dropout", 0.1))
            model = _model_factory(
                embedding_dim=int(payload["embedding_dim"]),
                shared_dim=shared_dim,
                dropout=dropout,
            )
            model.load_state_dict(state, strict=True)
            self._models.append(model.requires_grad_(False).eval().to(self.device))
            self._temperatures.append(float(payload["temperature"]))
            checkpoint_hashes.append(_sha256_file(path))

        identity_bytes = "\n".join(
            [
                _sha256_file(self.manifest_path), observed_encoder_sha,
                *checkpoint_hashes,
            ]
        ).encode("ascii")
        self.ensemble_id = "brightness-robust-all-extreme-cv36-" + hashlib.sha256(
            identity_bytes
        ).hexdigest()[:16]
        self.model_path = self.artifact_root / "conditional_four_output_heads"
        self.checkpoint_count = len(self._models)
        self.bundle_family = BUNDLE_FAMILY
        self.brightness_policy = BRIGHTNESS_POLICY

    @staticmethod
    def _validate_checkpoint(
        payload: dict, path: Path, expected_cell: Optional[str] = None,
        encoder_sha: Optional[str] = None,
    ) -> None:
        required = {
            "model_state_dict", "embedding_dim", "temperature",
            "cache", "brightness_policy", "output_semantics",
        }
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise ValueError(f"Malformed weak-primary checkpoint: {path}")
        temperature = float(payload["temperature"])
        if (
            payload.get("checkpoint_family") != BUNDLE_FAMILY
            or payload.get("execution_scope") != EXECUTION_SCOPE
            or payload.get("deployment_eligible") is not False
            or not math.isclose(float(payload.get("lambda_pair", -1)), LAMBDA_PAIR)
            or tuple(payload.get("classes", ())) != MODEL_CLASSES
            or payload.get("output_semantics")
            != "conditional_primary_probability_not_surface_fraction"
            or payload.get("brightness_policy") != BRIGHTNESS_POLICY
            or int(payload.get("embedding_dim", 0)) != 512
            or not math.isfinite(temperature)
            or temperature <= 0
        ):
            raise ValueError(
                f"Checkpoint violates brightness-robust shadow contract: {path}"
            )
        if expected_cell is not None:
            match = CHECKPOINT_RE.fullmatch(expected_cell)
            identity = (
                int(payload.get("fold", -1)),
                str(payload.get("held_out_pair_run", "")),
                int(payload.get("seed", -1)),
            )
            expected_identity = (
                int(match.group("fold")), match.group("pair"),
                int(match.group("seed")),
            )
            if identity != expected_identity:
                raise ValueError(f"Checkpoint identity mismatch: {path}")
        cache_encoder_sha = (
            payload.get("cache", {}).get("encoder", {}).get("checkpoint_sha256")
        )
        if encoder_sha is not None and cache_encoder_sha != encoder_sha:
            raise ValueError(f"Checkpoint encoder mismatch: {path}")

    def _preprocess(self, frame: np.ndarray):
        torch = self._torch
        if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3):
            raise ValueError("RHEED frame must be a 2D/3D numpy array.")
        array = np.asarray(frame)
        if array.ndim == 3:
            if array.shape[2] < 3:
                raise ValueError("Colour RHEED frames require three channels.")
            rgb = array[..., :3].astype(np.float32)
            scale = 65535.0 if array.dtype == np.uint16 else 255.0
            luminance = (
                0.299 * rgb[..., 0] + 0.587 * rgb[..., 1]
                + 0.114 * rgb[..., 2]
            ) / scale
        else:
            scale = 65535.0 if array.dtype == np.uint16 else 255.0
            luminance = array.astype(np.float32) / scale
        if not np.isfinite(luminance).all():
            raise ValueError("RHEED frame contains non-finite values.")
        image = torch.from_numpy(
            np.ascontiguousarray(np.clip(luminance, 0.0, 1.0))
        )[None, None]
        image = torch.nn.functional.interpolate(
            image, size=(224, 224), mode="bicubic", align_corners=False,
            antialias=True,
        ).clamp(0.0, 1.0)
        return ((image.repeat(1, 3, 1, 1) - 0.5) / 0.25).to(self.device)

    def classify(self, frame: np.ndarray) -> dict[str, object]:
        torch = self._torch
        with torch.inference_mode():
            embedding = self._encoder(self._preprocess(frame)).flatten(1).float()
            embedding = torch.nn.functional.normalize(embedding, dim=1)
            by_checkpoint = []
            applicability = []
            for model, temperature in zip(self._models, self._temperatures):
                output = model(embedding)
                by_checkpoint.append(
                    torch.softmax(output.primary_logits / temperature, dim=1)
                )
                applicability.append(torch.sigmoid(output.applicability_logits))
            probabilities = torch.stack(by_checkpoint)[:, 0]
            mean = probabilities.mean(dim=0)
            mean_applicability = torch.stack(applicability)[:, 0].mean(dim=0)
            entropy = -(mean * mean.clamp_min(1e-12).log()).sum()
            per_entropy = -(
                probabilities * probabilities.clamp_min(1e-12).log()
            ).sum(dim=1)
            disagreement = (entropy - per_entropy.mean()) / math.log(4.0)
            winner = int(mean.argmax())
        return {
            "conditional_probabilities": {
                label: float(mean[index])
                for index, label in enumerate(DISPLAY_CLASSES)
            },
            "predicted_class": DISPLAY_CLASSES[winner],
            "predicted_applicability": float(mean_applicability[winner]),
            "normalized_entropy": float(entropy / math.log(4.0)),
            "checkpoint_disagreement": float(disagreement.clamp(0.0, 1.0)),
            "checkpoint_count": self.checkpoint_count,
            "ensemble_id": self.ensemble_id,
            "bundle_family": self.bundle_family,
            "brightness_policy": self.brightness_policy,
            "output_classes": DISPLAY_CLASSES,
            "lambda_pair": LAMBDA_PAIR,
            "execution_scope": EXECUTION_SCOPE,
            "actionable": False,
            "abstain_reason": (
                "weak_shadow_only_no_registered_real_frame_actionability_policy"
            ),
        }
