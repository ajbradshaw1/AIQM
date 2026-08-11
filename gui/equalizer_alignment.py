"""Qt-free RHEED Equalizer basis and camera-alignment primitives.

The canonical coordinate frame is the committed simulator output at 128x96.
Alignment never changes that source data.  Instead, a grower-reviewed
``AlignmentCandidate`` maps every active simulator basis image into one frozen
camera frame.  Reflection (camera handedness) is explicit rather than hidden
inside a rotation.

This module intentionally contains no UI policy.  In particular, detecting
three spots produces a candidate, never an accepted calibration.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import time
from typing import Any, Mapping, Sequence
import uuid

import numpy as np


PROCESS_W = 128
PROCESS_H = 96
PROCESS_WH = (PROCESS_W, PROCESS_H)

ACTIVE_SIMULATOR_LABELS = ("1x1", "Tw(2x1)", "c(6x2)", "RT13")
UNAVAILABLE_LABELS = ("HTR",)
ORIENTATION_EVIDENCE_KINDS = ("Tw(2x1)", "c(6x2)", "RT13", "streak/tail")
CANONICAL_COORDINATE_FRAME = "simulator-128x96-v2"

MIN_PEAK_SEPARATION = 8.0
MIN_TRIANGLE_AREA_PX2 = 8.0
MIN_DETECTION_SNR = 6.0

MAX_RMS_RESIDUAL_PX = 3.0
MAX_REPROJECTION_PX = 5.0
MIN_SCALE = 0.5
MAX_SCALE = 2.0
MIN_VALID_COVERAGE = 0.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readonly_array(
    value: np.ndarray | Sequence[Sequence[float]],
    *,
    dtype: np.dtype | type | None = None,
) -> np.ndarray:
    """Return a contiguous array backed by an immutable bytes buffer.

    Merely clearing NumPy's ``WRITEABLE`` flag is not a hard immutability
    boundary when the array owns its allocation: callers can turn that flag
    back on.  Rebuilding the view from ``bytes`` makes the ultimate buffer
    immutable, so frozen calibration frames cannot be changed after review.
    """
    copied = np.array(value, dtype=dtype, copy=True, order="C")
    frozen = np.frombuffer(copied.tobytes(order="C"), dtype=copied.dtype)
    return frozen.reshape(copied.shape)


def normalize_utc_timestamp(value: object, *, field_name: str) -> str:
    """Validate and normalize one ISO-8601 timestamp in UTC.

    Provenance timestamps must include an explicit timezone and already
    represent UTC.  Normalization gives CSV, JSONL, and compatibility checks
    one spelling (``+00:00``) while rejecting local/naive timestamps.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be expressed in UTC")
    return parsed.astimezone(timezone.utc).isoformat()


def _array_sha256(array: np.ndarray) -> str:
    arr = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(arr.shape).encode("ascii"))
    digest.update(arr.dtype.str.encode("ascii"))
    digest.update(arr.tobytes(order="C"))
    return digest.hexdigest()


def _matrix23(value: np.ndarray | Sequence[Sequence[float]]) -> np.ndarray:
    matrix = _readonly_array(value, dtype=np.float64)
    if matrix.shape != (2, 3):
        raise ValueError(f"Expected a 2x3 transform, got {matrix.shape}")
    return matrix


def _points32(value: np.ndarray | Sequence[Sequence[float]]) -> np.ndarray:
    points = _readonly_array(value, dtype=np.float64)
    if points.shape != (3, 2):
        raise ValueError(f"Expected three 2-D points, got {points.shape}")
    return points


@dataclass(frozen=True)
class LandmarkDetection:
    """Result of one automatic landmark-detection attempt.

    A failure has an empty ``points`` array and a non-empty ``reason``.  This
    makes failure observable without inventing plausible-looking landmarks.
    """

    success: bool
    points: np.ndarray = field(
        default_factory=lambda: np.empty((0, 2), dtype=np.float64)
    )
    method: str = ""
    confidence: float = 0.0
    reason: str = ""
    peak_snr: float = 0.0

    def __post_init__(self) -> None:
        points = _readonly_array(self.points, dtype=np.float64)
        if self.success and points.shape != (3, 2):
            raise ValueError("A successful detection must contain three points")
        if not self.success and points.size:
            raise ValueError("A failed detection cannot contain points")
        if not self.success and not self.reason:
            raise ValueError("A failed detection must explain why it failed")
        object.__setattr__(self, "points", points)
        object.__setattr__(self, "confidence", float(np.clip(self.confidence, 0.0, 1.0)))

    @classmethod
    def failure(cls, reason: str, *, method: str = "auto") -> "LandmarkDetection":
        return cls(False, method=method, reason=reason)

    @classmethod
    def detected(
        cls,
        points: np.ndarray,
        *,
        method: str,
        confidence: float,
        peak_snr: float,
    ) -> "LandmarkDetection":
        return cls(
            True,
            points=points,
            method=method,
            confidence=confidence,
            peak_snr=peak_snr,
        )


@dataclass(frozen=True)
class RheedFrameSnapshot:
    """A frozen camera frame and the provenance received with that frame."""

    rgb: np.ndarray
    grayscale: np.ndarray
    captured_at_utc: str = ""
    received_monotonic_ns: int = 0
    capture_sequence: int = 0
    source_hwnd: int = 0
    capture_backend: str = ""
    capture_geometry_id: str = ""
    camera_width: int = 0
    camera_height: int = 0
    session_id: str = ""
    view_segment_id: int | None = None
    visual_history_generation: int = 0
    gun_aligned: bool = False
    realignment_active: bool = False
    source_frame_age_ms: float | None = None
    retrospective: bool = False

    def __post_init__(self) -> None:
        rgb = _readonly_array(self.rgb, dtype=np.uint8)
        gray = _readonly_array(self.grayscale, dtype=np.float32)
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"RGB snapshot must be HxWx3, got {rgb.shape}")
        if gray.shape != (PROCESS_H, PROCESS_W):
            raise ValueError(
                f"Processed snapshot must be {PROCESS_H}x{PROCESS_W}, got {gray.shape}"
            )
        object.__setattr__(self, "rgb", rgb)
        object.__setattr__(self, "grayscale", gray)
        width = int(self.camera_width or rgb.shape[1])
        height = int(self.camera_height or rgb.shape[0])
        if (width, height) != (rgb.shape[1], rgb.shape[0]):
            raise ValueError(
                "Snapshot camera dimensions do not match its RGB image"
            )
        object.__setattr__(self, "camera_width", width)
        object.__setattr__(self, "camera_height", height)
        if self.captured_at_utc:
            object.__setattr__(
                self,
                "captured_at_utc",
                normalize_utc_timestamp(
                    self.captured_at_utc,
                    field_name="captured_at_utc",
                ),
            )
        if self.source_frame_age_ms is not None:
            age = float(self.source_frame_age_ms)
            if not math.isfinite(age) or age < 0:
                raise ValueError("Source frame age must be finite and non-negative")
            object.__setattr__(self, "source_frame_age_ms", age)

    @classmethod
    def freeze(
        cls,
        rgb: np.ndarray,
        grayscale: np.ndarray,
        **provenance: object,
    ) -> "RheedFrameSnapshot":
        """Copy arrays now so later camera callbacks cannot mutate calibration."""
        return cls(rgb=np.array(rgb, copy=True), grayscale=np.array(grayscale, copy=True), **provenance)

    @classmethod
    def from_capture(
        cls,
        rgb: np.ndarray,
        grayscale: np.ndarray,
        metadata: Mapping[str, object],
        *,
        session_id: str = "",
        view_segment_id: int | None = None,
        visual_history_generation: int = 0,
        gun_aligned: bool = False,
        realignment_active: bool = False,
    ) -> "RheedFrameSnapshot":
        """Freeze a frame from camera metadata, accepting the WGC clock alias."""
        monotonic = metadata.get("received_monotonic_ns")
        if monotonic in (None, ""):
            monotonic = metadata.get("captured_monotonic_ns", 0)
        return cls.freeze(
            rgb,
            grayscale,
            captured_at_utc=str(metadata.get("captured_at_utc", "") or ""),
            received_monotonic_ns=int(monotonic or 0),
            capture_sequence=int(metadata.get("capture_sequence", 0) or 0),
            source_hwnd=int(metadata.get("source_hwnd", 0) or 0),
            capture_backend=str(metadata.get("capture_backend", "") or ""),
            capture_geometry_id=str(
                metadata.get("capture_geometry_id", "") or ""
            ),
            camera_width=int(metadata.get("camera_width", np.asarray(rgb).shape[1]) or 0),
            camera_height=int(metadata.get("camera_height", np.asarray(rgb).shape[0]) or 0),
            session_id=session_id,
            view_segment_id=view_segment_id,
            visual_history_generation=visual_history_generation,
            gun_aligned=gun_aligned,
            realignment_active=realignment_active,
            source_frame_age_ms=(
                None
                if metadata.get("frame_age_ms") in (None, "")
                else float(metadata["frame_age_ms"])
            ),
            retrospective=bool(metadata.get("retrospective", False)),
        )

    def age_ms(self, now_monotonic_ns: int | None = None) -> float:
        """Recompute age from the monotonic receive time; never reuse cached age."""
        if self.received_monotonic_ns <= 0:
            return math.inf
        now = time.monotonic_ns() if now_monotonic_ns is None else int(now_monotonic_ns)
        return max(0.0, (now - self.received_monotonic_ns) / 1_000_000.0)

    def logging_age_ms(self) -> float:
        """Use persisted source age for historical frames across reboots."""
        if self.retrospective:
            if self.source_frame_age_ms is None:
                return math.inf
            return self.source_frame_age_ms
        return self.age_ms()

    def orientation_evidence_png(self) -> bytes:
        """Encode the frozen RGB pixels as the exact lossless evidence file."""
        from PIL import Image

        output = io.BytesIO()
        Image.fromarray(self.rgb).save(
            output,
            format="PNG",
            optimize=False,
            compress_level=6,
        )
        return output.getvalue()

    def orientation_evidence_sha256(self) -> str:
        """SHA-256 of :meth:`orientation_evidence_png`, not a live frame."""
        return hashlib.sha256(self.orientation_evidence_png()).hexdigest()


@dataclass(frozen=True)
class BasisAsset:
    """One canonical basis image or an explicitly unavailable class."""

    label: str
    image: np.ndarray | None
    active: bool
    source_kind: str
    source_path: str = ""
    unavailable_reason: str = ""
    source_transform: np.ndarray = field(default_factory=lambda: np.eye(3, dtype=np.float64))
    image_sha256: str = ""

    def __post_init__(self) -> None:
        transform = _readonly_array(self.source_transform, dtype=np.float64)
        if transform.shape != (3, 3):
            raise ValueError("Basis source_transform must be 3x3")
        object.__setattr__(self, "source_transform", transform)

        if self.active:
            if self.image is None:
                raise ValueError(f"Active basis {self.label!r} has no image")
            image = _readonly_array(self.image, dtype=np.float32)
            if image.shape != (PROCESS_H, PROCESS_W):
                raise ValueError(
                    f"Basis {self.label!r} must be {PROCESS_H}x{PROCESS_W}, got {image.shape}"
                )
            if self.source_kind != "simulator":
                raise ValueError(f"Active basis {self.label!r} must come from simulator")
            if not np.allclose(transform, np.eye(3), atol=1e-12):
                raise ValueError("Simulator assets must use the canonical identity transform")
            object.__setattr__(self, "image", image)
            expected_hash = _array_sha256(image)
            if self.image_sha256 and self.image_sha256 != expected_hash:
                raise ValueError(f"Basis hash mismatch for {self.label!r}")
            object.__setattr__(self, "image_sha256", expected_hash)
        else:
            if not self.unavailable_reason:
                raise ValueError(f"Inactive basis {self.label!r} needs an unavailable reason")
            if self.image is not None:
                object.__setattr__(self, "image", _readonly_array(self.image, dtype=np.float32))


@dataclass(frozen=True)
class BasisBundle:
    """Versioned canonical basis with a content-addressed identity."""

    assets: tuple[BasisAsset, ...]
    version: str = "simulator-v2"
    coordinate_frame: str = CANONICAL_COORDINATE_FRAME
    width: int = PROCESS_W
    height: int = PROCESS_H
    bundle_id: str = ""

    def __post_init__(self) -> None:
        assets = tuple(self.assets)
        labels = tuple(asset.label for asset in assets)
        if len(set(labels)) != len(labels):
            raise ValueError("Basis bundle labels must be unique")
        active = tuple(asset.label for asset in assets if asset.active)
        if active != ACTIVE_SIMULATOR_LABELS:
            raise ValueError(
                f"active classes must be {ACTIVE_SIMULATOR_LABELS}, got {active}"
            )
        htr = next((asset for asset in assets if asset.label == "HTR"), None)
        if htr is None or htr.active:
            raise ValueError("HTR must be present but inactive in this bundle")
        if (self.width, self.height) != PROCESS_WH:
            raise ValueError(f"Canonical bundle size must be {PROCESS_WH}")
        object.__setattr__(self, "assets", assets)

        payload = self._manifest_payload()
        computed = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.bundle_id and self.bundle_id != computed:
            raise ValueError("Basis bundle identity does not match its contents")
        object.__setattr__(self, "bundle_id", computed)

    def _manifest_payload(self) -> dict[str, Any]:
        """Return the content-addressed portion of the audit manifest."""
        return {
            "version": self.version,
            "coordinate_frame": self.coordinate_frame,
            "width": self.width,
            "height": self.height,
            "assets": [
                {
                    "label": a.label,
                    "active": a.active,
                    "source_kind": a.source_kind,
                    "source_path": a.source_path,
                    "unavailable_reason": a.unavailable_reason,
                    "source_transform": np.asarray(a.source_transform).tolist(),
                    "image_sha256": a.image_sha256,
                }
                for a in self.assets
            ],
        }

    def to_manifest_dict(self) -> dict[str, Any]:
        """Return the complete JSON-safe manifest persisted with a session."""
        return {
            "basis_manifest_schema_version": 1,
            "bundle_id": self.bundle_id,
            **self._manifest_payload(),
        }

    @classmethod
    def validate_manifest_dict(cls, value: Mapping[str, object]) -> dict[str, Any]:
        """Validate a persisted manifest without requiring the image files.

        The per-asset hashes are the durable link to the pixels.  Replay can
        therefore audit old calibration records even if the installed bundle
        has since changed or those source paths are no longer available.
        """
        if not isinstance(value, Mapping):
            raise ValueError("Basis bundle manifest must be an object")
        manifest = dict(value)
        if manifest.get("basis_manifest_schema_version") != 1:
            raise ValueError("Unsupported basis bundle manifest schema")
        content_keys = (
            "version",
            "coordinate_frame",
            "width",
            "height",
            "assets",
        )
        if any(key not in manifest for key in content_keys):
            raise ValueError("Basis bundle manifest is incomplete")
        assets = manifest["assets"]
        if not isinstance(assets, list):
            raise ValueError("Basis bundle assets must be a list")
        labels: list[str] = []
        active_labels: list[str] = []
        for asset in assets:
            if not isinstance(asset, Mapping):
                raise ValueError("Basis bundle asset manifest must be an object")
            required = {
                "label",
                "active",
                "source_kind",
                "source_path",
                "unavailable_reason",
                "source_transform",
                "image_sha256",
            }
            if set(asset) != required:
                raise ValueError("Basis bundle asset manifest fields are invalid")
            label = str(asset["label"])
            labels.append(label)
            if type(asset["active"]) is not bool:
                raise ValueError("Basis bundle asset active flag must be boolean")
            if asset["active"]:
                active_labels.append(label)
                digest = str(asset["image_sha256"]).lower()
                if len(digest) != 64 or any(
                    char not in "0123456789abcdef" for char in digest
                ):
                    raise ValueError("Active basis asset SHA-256 is invalid")
                if str(asset["source_kind"]) != "simulator":
                    raise ValueError("Active basis asset is not simulator-derived")
                transform = np.asarray(asset["source_transform"], dtype=np.float64)
                if transform.shape != (3, 3) or not np.allclose(
                    transform,
                    np.eye(3),
                    atol=1e-12,
                ):
                    raise ValueError("Active simulator basis transform is not identity")
        if tuple(labels) != (*ACTIVE_SIMULATOR_LABELS, "HTR"):
            raise ValueError("Basis bundle manifest labels are invalid")
        if tuple(active_labels) != ACTIVE_SIMULATOR_LABELS:
            raise ValueError("Basis bundle manifest active classes are invalid")
        if (int(manifest["width"]), int(manifest["height"])) != PROCESS_WH:
            raise ValueError("Basis bundle manifest dimensions are invalid")
        payload = {key: manifest[key] for key in content_keys}
        computed = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if str(manifest.get("bundle_id", "")) != computed:
            raise ValueError("Basis bundle manifest identity does not match contents")
        return manifest

    @property
    def active_labels(self) -> tuple[str, ...]:
        return tuple(asset.label for asset in self.assets if asset.active)

    @property
    def unavailable_labels(self) -> tuple[str, ...]:
        return tuple(asset.label for asset in self.assets if not asset.active)

    def active_images(self, *, copy: bool = False) -> dict[str, np.ndarray]:
        return {
            asset.label: np.array(asset.image, copy=True) if copy else asset.image
            for asset in self.assets
            if asset.active and asset.image is not None
        }

    def asset(self, label: str) -> BasisAsset:
        for asset in self.assets:
            if asset.label == label:
                return asset
        raise KeyError(label)


def _validate_similarity_matrix_semantics(
    matrix: np.ndarray,
    *,
    parity: str,
    scale: float,
    rotation_deg: float,
) -> None:
    """Require the stored affine matrix to be the declared similarity."""
    linear = np.asarray(matrix, dtype=np.float64)[:, :2]
    if not np.isfinite(linear).all() or not math.isfinite(scale) or scale <= 0:
        raise ValueError("Similarity matrix and scale must be finite and positive")
    gram = linear.T @ linear
    if not np.allclose(
        gram,
        (float(scale) ** 2) * np.eye(2),
        rtol=1e-7,
        atol=1e-9,
    ):
        raise ValueError("Alignment matrix contains shear or anisotropic scale")
    determinant = float(np.linalg.det(linear))
    expected_sign = 1.0 if parity == "normal" else -1.0
    if determinant * expected_sign <= 0:
        raise ValueError("Alignment matrix determinant contradicts its parity")
    parity_linear = (
        np.eye(2, dtype=np.float64)
        if parity == "normal"
        else np.diag([-1.0, 1.0])
    )
    proper_rotation = (linear @ parity_linear) / float(scale)
    computed_rotation = math.degrees(
        math.atan2(proper_rotation[1, 0], proper_rotation[0, 0])
    )
    delta = (computed_rotation - float(rotation_deg) + 180.0) % 360.0 - 180.0
    if not math.isfinite(rotation_deg) or abs(delta) > 1e-6:
        raise ValueError("Stored rotation contradicts the alignment matrix")


@dataclass(frozen=True)
class AlignmentCandidate:
    """One unaccepted correspondence/parity hypothesis."""

    matrix: np.ndarray
    parity: str
    endpoint_order: str
    basis_points: np.ndarray
    live_points: np.ndarray
    projected_points: np.ndarray
    residuals_px: np.ndarray
    scale: float
    rotation_deg: float
    translation_xy: tuple[float, float]
    rms_residual_px: float
    max_residual_px: float
    valid_coverage: float
    correlation: float
    valid: bool
    validation_reason: str = ""
    equivalent_hypotheses: tuple[str, ...] = ()
    candidate_id: str = ""

    def __post_init__(self) -> None:
        matrix = _matrix23(self.matrix)
        basis = _points32(self.basis_points)
        live = _points32(self.live_points)
        projected = _points32(self.projected_points)
        residuals = _readonly_array(self.residuals_px, dtype=np.float64)
        if residuals.shape != (3,):
            raise ValueError("Candidate residuals must contain three values")
        object.__setattr__(self, "matrix", matrix)
        object.__setattr__(self, "basis_points", basis)
        object.__setattr__(self, "live_points", live)
        object.__setattr__(self, "projected_points", projected)
        object.__setattr__(self, "residuals_px", residuals)
        if self.parity not in {"normal", "mirrored"}:
            raise ValueError(f"Unknown parity {self.parity!r}")
        if self.endpoint_order not in {"forward", "reversed"}:
            raise ValueError(f"Unknown endpoint order {self.endpoint_order!r}")
        projected_expected = (
            matrix
            @ np.column_stack([basis, np.ones(3, dtype=np.float64)]).T
        ).T
        residuals_expected = np.linalg.norm(projected_expected - live, axis=1)
        if not np.allclose(projected, projected_expected, atol=1e-6, rtol=0.0):
            raise ValueError("Candidate projected points do not match its matrix")
        if not np.allclose(residuals, residuals_expected, atol=1e-6, rtol=0.0):
            raise ValueError("Candidate residuals do not match its geometry")
        computed_rms = float(np.sqrt(np.mean(residuals_expected**2)))
        computed_max = float(np.max(residuals_expected))
        if not math.isclose(self.rms_residual_px, computed_rms, abs_tol=1e-6):
            raise ValueError("Candidate RMS residual is inconsistent")
        if not math.isclose(self.max_residual_px, computed_max, abs_tol=1e-6):
            raise ValueError("Candidate maximum residual is inconsistent")
        determinant = float(np.linalg.det(matrix[:, :2]))
        computed_scale = math.sqrt(abs(determinant)) if math.isfinite(determinant) else math.nan
        if not math.isclose(self.scale, computed_scale, abs_tol=1e-9):
            raise ValueError("Candidate scale is inconsistent with its matrix")
        translation = tuple(float(value) for value in self.translation_xy)
        if len(translation) != 2 or not np.isfinite(translation).all():
            raise ValueError("Candidate translation must contain two finite values")
        if not np.allclose(translation, matrix[:, 2], atol=1e-9, rtol=0.0):
            raise ValueError("Candidate translation is inconsistent with its matrix")
        if not math.isfinite(self.rotation_deg):
            raise ValueError("Candidate rotation is not finite")
        if not math.isfinite(self.correlation) or not -1.0 <= self.correlation <= 1.0:
            raise ValueError("Candidate correlation must be finite in [-1, 1]")
        _validate_similarity_matrix_semantics(
            matrix,
            parity=self.parity,
            scale=self.scale,
            rotation_deg=self.rotation_deg,
        )
        expected_valid, expected_reason = validate_alignment(
            matrix,
            scale=self.scale,
            rms_residual_px=self.rms_residual_px,
            max_residual_px=self.max_residual_px,
            valid_coverage=self.valid_coverage,
        )
        if bool(self.valid) != expected_valid:
            raise ValueError("Candidate valid flag contradicts its metrics")
        if not expected_valid and not (self.validation_reason or expected_reason):
            raise ValueError("Invalid candidate must include a failure reason")
        hypotheses = self.equivalent_hypotheses or (
            f"{self.parity}:{self.endpoint_order}",
        )
        object.__setattr__(self, "equivalent_hypotheses", tuple(hypotheses))
        digest = hashlib.sha256()
        digest.update(self.parity.encode("ascii"))
        digest.update(self.endpoint_order.encode("ascii"))
        digest.update(np.ascontiguousarray(matrix).tobytes())
        digest.update(np.ascontiguousarray(live).tobytes())
        computed = digest.hexdigest()[:24]
        if self.candidate_id and self.candidate_id != computed:
            raise ValueError("Candidate identity does not match its geometry")
        object.__setattr__(self, "candidate_id", computed)

    @property
    def dx(self) -> float:
        return self.translation_xy[0]

    @property
    def dy(self) -> float:
        return self.translation_xy[1]


@dataclass(frozen=True)
class CalibrationRecord:
    """Accepted-or-pending calibration suitable for lifecycle and logging."""

    calibration_id: str
    basis_bundle_id: str
    candidate_id: str
    matrix: np.ndarray
    parity: str
    endpoint_order: str
    basis_points: np.ndarray
    live_points: np.ndarray
    residuals_px: np.ndarray
    rotation_deg: float
    scale: float
    rms_residual_px: float
    max_residual_px: float
    valid_coverage: float
    correlation: float
    active_classes: tuple[str, ...] = ACTIVE_SIMULATOR_LABELS
    created_at_utc: str = field(default_factory=_utc_now)
    source_hwnd: int = 0
    camera_width: int = 0
    camera_height: int = 0
    capture_backend: str = ""
    capture_geometry_id: str = ""
    captured_at_utc: str = ""
    capture_sequence: int = 0
    received_monotonic_ns: int = 0
    session_id: str = ""
    view_segment_id: int | None = None
    visual_history_generation: int = 0
    gun_aligned: bool = False
    realignment_active: bool = False
    grower_accepted: bool = False
    accepted_by: str = ""
    orientation_evidence_kind: str = ""
    orientation_evidence_sha256: str = ""
    orientation_evidence_path: str = ""
    invalidated_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_at_utc",
            normalize_utc_timestamp(
                self.created_at_utc,
                field_name="created_at_utc",
            ),
        )
        if self.captured_at_utc:
            object.__setattr__(
                self,
                "captured_at_utc",
                normalize_utc_timestamp(
                    self.captured_at_utc,
                    field_name="captured_at_utc",
                ),
            )
        matrix = _matrix23(self.matrix)
        basis = _points32(self.basis_points)
        live = _points32(self.live_points)
        object.__setattr__(self, "matrix", matrix)
        object.__setattr__(self, "basis_points", basis)
        object.__setattr__(self, "live_points", live)
        residuals = _readonly_array(self.residuals_px, dtype=np.float64)
        if residuals.shape != (3,):
            raise ValueError("Calibration residuals must contain three values")
        object.__setattr__(self, "residuals_px", residuals)
        active_classes = tuple(self.active_classes)
        object.__setattr__(self, "active_classes", active_classes)

        if not self.calibration_id or not self.basis_bundle_id or not self.candidate_id:
            raise ValueError("Calibration identifiers must be non-empty")
        if self.parity not in {"normal", "mirrored"}:
            raise ValueError(f"Unknown calibration parity {self.parity!r}")
        if self.endpoint_order not in {"forward", "reversed"}:
            raise ValueError(f"Unknown endpoint order {self.endpoint_order!r}")
        if active_classes != ACTIVE_SIMULATOR_LABELS:
            raise ValueError(
                f"Calibration active classes must be {ACTIVE_SIMULATOR_LABELS}"
            )
        if not isinstance(self.grower_accepted, bool):
            raise ValueError("grower_accepted must be boolean")
        if self.grower_accepted and self.invalidated_reason:
            raise ValueError("An accepted calibration cannot be invalidated")
        evidence_fields = (
            self.orientation_evidence_kind,
            self.orientation_evidence_sha256,
            self.orientation_evidence_path,
        )
        if any(evidence_fields) and not all(evidence_fields):
            raise ValueError("Orientation evidence fields must be recorded together")
        if self.orientation_evidence_kind and (
            self.orientation_evidence_kind not in ORIENTATION_EVIDENCE_KINDS
        ):
            raise ValueError(
                f"Unsupported orientation evidence {self.orientation_evidence_kind!r}"
            )
        if self.orientation_evidence_sha256:
            digest = self.orientation_evidence_sha256.lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("Orientation evidence SHA-256 is invalid")
        if self.orientation_evidence_path:
            expected_path = (
                f"frames/equalizer_calibration_{self.calibration_id}_orientation.png"
            )
            if self.orientation_evidence_path.replace("\\", "/") != expected_path:
                raise ValueError("Orientation evidence path is not calibration-bound")

        projected = (
            matrix
            @ np.column_stack([basis, np.ones(3, dtype=np.float64)]).T
        ).T
        expected_residuals = np.linalg.norm(projected - live, axis=1)
        if not np.allclose(residuals, expected_residuals, atol=1e-6, rtol=0.0):
            raise ValueError("Calibration residuals do not match its geometry")
        computed_rms = float(np.sqrt(np.mean(expected_residuals**2)))
        computed_max = float(np.max(expected_residuals))
        if not math.isclose(self.rms_residual_px, computed_rms, abs_tol=1e-6):
            raise ValueError("Calibration RMS residual is inconsistent")
        if not math.isclose(self.max_residual_px, computed_max, abs_tol=1e-6):
            raise ValueError("Calibration maximum residual is inconsistent")
        determinant = float(np.linalg.det(matrix[:, :2]))
        computed_scale = math.sqrt(abs(determinant)) if math.isfinite(determinant) else math.nan
        if not math.isclose(self.scale, computed_scale, abs_tol=1e-9):
            raise ValueError("Calibration scale is inconsistent with its matrix")
        if not math.isfinite(self.rotation_deg):
            raise ValueError("Calibration rotation is not finite")
        if not math.isfinite(self.correlation) or not -1.0 <= self.correlation <= 1.0:
            raise ValueError("Calibration correlation must be finite in [-1, 1]")
        _validate_similarity_matrix_semantics(
            matrix,
            parity=self.parity,
            scale=self.scale,
            rotation_deg=self.rotation_deg,
        )
        valid, reason = validate_alignment(
            matrix,
            scale=self.scale,
            rms_residual_px=self.rms_residual_px,
            max_residual_px=self.max_residual_px,
            valid_coverage=self.valid_coverage,
        )
        if not valid:
            raise ValueError(f"Calibration geometry is invalid: {reason}")
        local_demo = str(self.capture_backend or "").startswith("dummy")
        if self.grower_accepted and (
            not self.captured_at_utc
            or self.capture_sequence <= 0
            or self.received_monotonic_ns <= 0
            or not self.capture_backend
            or not self.capture_geometry_id
            or self.camera_width <= 0
            or self.camera_height <= 0
            or not all(evidence_fields)
        ):
            raise ValueError("Accepted calibration lacks capture/evidence provenance")
        if self.grower_accepted and not local_demo and (
            not self.session_id
            or self.view_segment_id is None
            or not self.gun_aligned
            or self.realignment_active
        ):
            raise ValueError("Accepted calibration lacks stable session/QC provenance")

    @classmethod
    def from_candidate(
        cls,
        candidate: AlignmentCandidate,
        bundle: BasisBundle,
        snapshot: RheedFrameSnapshot,
        *,
        grower_accepted: bool = False,
    ) -> "CalibrationRecord":
        if not candidate.valid:
            raise ValueError(f"Cannot create calibration from invalid candidate: {candidate.validation_reason}")
        return cls(
            calibration_id=str(uuid.uuid4()),
            basis_bundle_id=bundle.bundle_id,
            candidate_id=candidate.candidate_id,
            matrix=candidate.matrix,
            parity=candidate.parity,
            endpoint_order=candidate.endpoint_order,
            basis_points=candidate.basis_points,
            live_points=candidate.live_points,
            residuals_px=candidate.residuals_px,
            rotation_deg=candidate.rotation_deg,
            scale=candidate.scale,
            rms_residual_px=candidate.rms_residual_px,
            max_residual_px=candidate.max_residual_px,
            valid_coverage=candidate.valid_coverage,
            correlation=candidate.correlation,
            active_classes=bundle.active_labels,
            source_hwnd=snapshot.source_hwnd,
            camera_width=snapshot.camera_width,
            camera_height=snapshot.camera_height,
            capture_backend=snapshot.capture_backend,
            capture_geometry_id=snapshot.capture_geometry_id,
            captured_at_utc=snapshot.captured_at_utc,
            capture_sequence=snapshot.capture_sequence,
            received_monotonic_ns=snapshot.received_monotonic_ns,
            session_id=snapshot.session_id,
            view_segment_id=snapshot.view_segment_id,
            visual_history_generation=snapshot.visual_history_generation,
            gun_aligned=snapshot.gun_aligned,
            realignment_active=snapshot.realignment_active,
            grower_accepted=grower_accepted,
        )

    def accepted(
        self,
        accepted_by: str = "",
        orientation_evidence_sha256: str = "",
        *,
        orientation_evidence_kind: str = "",
        orientation_evidence_path: str = "",
    ) -> "CalibrationRecord":
        if self.invalidated_reason:
            raise ValueError(
                "An invalidated calibration cannot be revived; recalibrate"
            )
        if not orientation_evidence_path:
            orientation_evidence_path = (
                f"frames/equalizer_calibration_{self.calibration_id}_orientation.png"
            )
        return replace(
            self,
            grower_accepted=True,
            accepted_by=accepted_by,
            orientation_evidence_kind=orientation_evidence_kind,
            orientation_evidence_sha256=orientation_evidence_sha256,
            orientation_evidence_path=orientation_evidence_path,
            invalidated_reason="",
        )

    def invalidated(self, reason: str) -> "CalibrationRecord":
        if not reason:
            raise ValueError("Invalidation requires a reason")
        return replace(self, grower_accepted=False, invalidated_reason=reason)

    @property
    def camera_mode(self) -> str:
        """Compatibility spelling for the former calibration object."""
        return self.capture_backend

    @property
    def reprojection_px(self) -> float:
        return self.rms_residual_px

    @property
    def max_disagreement_px(self) -> float:
        return self.max_residual_px

    @property
    def offset(self) -> tuple[float, float]:
        return float(self.matrix[0, 2]), float(self.matrix[1, 2])

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, complete calibration journal record."""
        return {
            "calibration_id": self.calibration_id,
            "basis_bundle_id": self.basis_bundle_id,
            "candidate_id": self.candidate_id,
            "matrix": self.matrix.tolist(),
            "parity": self.parity,
            "endpoint_order": self.endpoint_order,
            "basis_points": self.basis_points.tolist(),
            "live_points": self.live_points.tolist(),
            "residuals_px": self.residuals_px.tolist(),
            "rotation_deg": self.rotation_deg,
            "scale": self.scale,
            "rms_residual_px": self.rms_residual_px,
            "max_residual_px": self.max_residual_px,
            "valid_coverage": self.valid_coverage,
            "correlation": self.correlation,
            "active_classes": list(self.active_classes),
            "created_at_utc": self.created_at_utc,
            "source_hwnd": self.source_hwnd,
            "camera_width": self.camera_width,
            "camera_height": self.camera_height,
            "capture_backend": self.capture_backend,
            "capture_geometry_id": self.capture_geometry_id,
            "captured_at_utc": self.captured_at_utc,
            "capture_sequence": self.capture_sequence,
            "received_monotonic_ns": self.received_monotonic_ns,
            "session_id": self.session_id,
            "view_segment_id": self.view_segment_id,
            "visual_history_generation": self.visual_history_generation,
            "gun_aligned": self.gun_aligned,
            "realignment_active": self.realignment_active,
            "grower_accepted": self.grower_accepted,
            "accepted_by": self.accepted_by,
            "orientation_evidence_kind": self.orientation_evidence_kind,
            "orientation_evidence_sha256": self.orientation_evidence_sha256,
            "orientation_evidence_path": self.orientation_evidence_path,
            "invalidated_reason": self.invalidated_reason,
        }

    @classmethod
    def from_json_dict(cls, payload: Mapping[str, object]) -> "CalibrationRecord":
        """Rehydrate a record written by :meth:`to_json_dict`."""
        values = dict(payload)
        values["matrix"] = np.asarray(values["matrix"], dtype=np.float64)
        values["basis_points"] = np.asarray(values["basis_points"], dtype=np.float64)
        values["live_points"] = np.asarray(values["live_points"], dtype=np.float64)
        values["residuals_px"] = np.asarray(values["residuals_px"], dtype=np.float64)
        values["active_classes"] = tuple(values.get("active_classes", ACTIVE_SIMULATOR_LABELS))
        return cls(**values)


# Transitional import name.  New code should use CalibrationRecord.
Calibration = CalibrationRecord


def _triangle_area(points: np.ndarray) -> float:
    a, b, c = points
    ab = b - a
    ac = c - a
    return abs(float(ab[0] * ac[1] - ab[1] * ac[0])) * 0.5


def _landmark_failure_reason(
    points: np.ndarray,
    *,
    image_shape: tuple[int, int] = (PROCESS_H, PROCESS_W),
) -> str:
    points = np.asarray(points, dtype=np.float64)
    if points.shape != (3, 2):
        return f"expected three 2-D landmarks, got {points.shape}"
    if not np.isfinite(points).all():
        return "landmarks contain non-finite coordinates"
    xs, ys = points[:, 0], points[:, 1]
    if not (xs[0] < xs[1] < xs[2]):
        return "landmarks are not ordered left, specular, right"
    if min(xs[1] - xs[0], xs[2] - xs[1]) < MIN_PEAK_SEPARATION:
        return "landmark peaks are too close"
    height, width = image_shape
    if np.any(xs < 0) or np.any(xs >= width) or np.any(ys < 0) or np.any(ys >= height):
        return "landmark lies outside the processed image"
    if _triangle_area(points) < MIN_TRIANGLE_AREA_PX2:
        return "landmarks are collinear or nearly collinear"
    return ""


def _landmarks_are_degenerate(points: np.ndarray) -> bool:
    return bool(_landmark_failure_reason(points))


def _gaussian_kernel1d(sigma: float) -> np.ndarray:
    radius = max(1, int(math.ceil(3.0 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-(x * x) / (2.0 * sigma * sigma))
    return kernel / kernel.sum()


def _smooth_image(image: np.ndarray, sigma: float = 2.0) -> np.ndarray:
    kernel = _gaussian_kernel1d(sigma)
    tmp = np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="same"), 1, image)
    return np.apply_along_axis(lambda col: np.convolve(col, kernel, mode="same"), 0, tmp)


def detect_landmarks(image: np.ndarray, *, method: str = "auto-2d") -> LandmarkDetection:
    """Detect three bright, separated 1x1 spots or return an explicit failure."""
    arr = np.asarray(image)
    if arr.ndim != 2:
        return LandmarkDetection.failure(f"expected a 2-D image, got {arr.shape}", method=method)
    if arr.shape[0] < 24 or arr.shape[1] < 32:
        return LandmarkDetection.failure(f"image is too small: {arr.shape}", method=method)
    if not np.isfinite(arr).all():
        return LandmarkDetection.failure("image contains non-finite pixels", method=method)

    values = arr.astype(np.float64)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    noise = max(1e-6, 1.4826 * mad)
    dynamic = float(np.percentile(values, 99.9) - median)
    if dynamic <= max(1e-6, MIN_DETECTION_SNR * noise):
        return LandmarkDetection.failure(
            f"insufficient spot contrast (peak SNR {dynamic / noise:.1f})",
            method=method,
        )

    smooth = _smooth_image(values, sigma=2.0)
    height, width = smooth.shape
    margin_x = max(6, int(round(width * 0.05)))
    margin_y = max(5, int(round(height * 0.05)))
    interior = smooth[margin_y:height - margin_y, margin_x:width - margin_x]
    if interior.size == 0:
        return LandmarkDetection.failure("image has no searchable interior", method=method)

    threshold = float(np.median(interior)) + max(4.0 * noise, 0.08 * dynamic)
    ys, xs = np.where(interior >= threshold)
    if len(xs) < 3:
        return LandmarkDetection.failure("fewer than three peak candidates", method=method)
    xs = xs + margin_x
    ys = ys + margin_y
    intensities = smooth[ys, xs]
    order = np.argsort(-intensities, kind="stable")

    chosen: list[tuple[float, float, float]] = []
    for index in order:
        x, y = int(xs[index]), int(ys[index])
        radius = 2
        patch = smooth[max(0, y - radius):y + radius + 1, max(0, x - radius):x + radius + 1]
        if smooth[y, x] < float(np.max(patch)):
            continue
        if any(math.hypot(x - px, y - py) < MIN_PEAK_SEPARATION * 1.5 for px, py, _ in chosen):
            continue
        if any(abs(x - px) < MIN_PEAK_SEPARATION for px, _, _ in chosen):
            continue
        chosen.append((float(x), float(y), float(smooth[y, x])))
        if len(chosen) == 3:
            break

    if len(chosen) != 3:
        return LandmarkDetection.failure("could not isolate three separated spots", method=method)

    chosen.sort(key=lambda item: item[0])
    points = np.array([(x, y) for x, y, _ in chosen], dtype=np.float64)
    reason = _landmark_failure_reason(points, image_shape=arr.shape)
    if reason:
        return LandmarkDetection.failure(reason, method=method)

    peak_snr = min((value - median) / noise for _, _, value in chosen)
    confidence = float(np.clip((peak_snr - MIN_DETECTION_SNR) / 12.0, 0.0, 1.0))
    return LandmarkDetection.detected(
        points,
        method=method,
        confidence=confidence,
        peak_snr=float(peak_snr),
    )


def detect_basis_landmarks(basis_1x1: np.ndarray) -> LandmarkDetection:
    return detect_landmarks(basis_1x1, method="canonical-basis-2d")


def detect_live_landmarks(live_frame: np.ndarray) -> LandmarkDetection:
    return detect_landmarks(live_frame, method="live-frame-2d")


def validate_manual_landmarks(
    image: np.ndarray,
    points: np.ndarray,
) -> LandmarkDetection:
    """Validate grower clicks without inventing or reordering landmarks."""
    arr = np.asarray(image)
    pts = np.asarray(points, dtype=np.float64)
    if arr.ndim != 2:
        return LandmarkDetection.failure(
            f"expected a 2-D image, got {arr.shape}", method="manual-grower",
        )
    if not np.isfinite(arr).all():
        return LandmarkDetection.failure(
            "image contains non-finite pixels", method="manual-grower",
        )
    reason = _landmark_failure_reason(pts, image_shape=arr.shape)
    if reason:
        return LandmarkDetection.failure(reason, method="manual-grower")

    values = arr.astype(np.float64)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    noise = max(1e-6, 1.4826 * mad)
    local_snr: list[float] = []
    for x, y in pts:
        xi, yi = int(round(x)), int(round(y))
        radius = 4
        patch = values[
            max(0, yi - radius):min(values.shape[0], yi + radius + 1),
            max(0, xi - radius):min(values.shape[1], xi + radius + 1),
        ]
        local_snr.append((float(np.max(patch)) - median) / noise)
    minimum_snr = min(local_snr)
    if minimum_snr < MIN_DETECTION_SNR:
        return LandmarkDetection.failure(
            f"manual landmark SNR {minimum_snr:.1f} is below "
            f"{MIN_DETECTION_SNR:.1f}",
            method="manual-grower",
        )
    confidence = float(
        np.clip((minimum_snr - MIN_DETECTION_SNR) / 12.0, 0.0, 1.0)
    )
    return LandmarkDetection.detected(
        pts,
        method="manual-grower",
        confidence=confidence,
        peak_snr=minimum_snr,
    )


def _coerce_points(value: LandmarkDetection | np.ndarray, name: str) -> np.ndarray:
    if isinstance(value, LandmarkDetection):
        if not value.success:
            raise ValueError(f"{name} detection failed: {value.reason}")
        points = np.asarray(value.points, dtype=np.float64)
    else:
        points = np.asarray(value, dtype=np.float64)
    if points.shape != (3, 2) or not np.isfinite(points).all():
        raise ValueError(f"{name} must contain three finite 2-D points")
    return points


def project_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float64)
    points = np.asarray(points, dtype=np.float64)
    homogeneous = np.column_stack([points, np.ones(len(points), dtype=np.float64)])
    return (matrix @ homogeneous.T).T


def compute_similarity(
    src: np.ndarray,
    dst: np.ndarray,
) -> tuple[np.ndarray, float, float, float, float]:
    """Compute a proper 2-D similarity mapping ``src`` to ``dst``.

    Reflection is deliberately not inferred here.  Callers represent it as
    an explicit source parity transform and compose the matrices.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 2 or len(src) < 2:
        raise ValueError("src and dst must be matching Nx2 point arrays")
    if not np.isfinite(src).all() or not np.isfinite(dst).all():
        raise ValueError("point coordinates must be finite")

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_c = src - src_mean
    dst_c = dst - dst_mean
    variance = float(np.sum(src_c * src_c) / len(src))
    if variance <= 1e-12:
        raise ValueError("source landmarks are degenerate")

    covariance = (dst_c.T @ src_c) / len(src)
    u, singular_values, vt = np.linalg.svd(covariance)
    sign = np.ones(2, dtype=np.float64)
    if np.linalg.det(u @ vt) < 0:
        sign[-1] = -1.0
    rotation = u @ np.diag(sign) @ vt
    scale = float(np.sum(singular_values * sign) / variance)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("similarity scale is not positive and finite")
    linear = scale * rotation
    translation = dst_mean - linear @ src_mean
    matrix = np.column_stack([linear, translation])
    angle = math.degrees(math.atan2(rotation[1, 0], rotation[0, 0]))
    return matrix, scale, angle, float(translation[0]), float(translation[1])


def _parity_matrix(parity: str, width: int = PROCESS_W) -> np.ndarray:
    if parity == "normal":
        return np.eye(3, dtype=np.float64)
    if parity != "mirrored":
        raise ValueError(f"Unknown parity {parity!r}")
    # x' = (width - 1) - x, reflection about the canonical image centre.
    return np.array(
        [[-1.0, 0.0, float(width - 1)], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _compose_affine(left: np.ndarray, right_homogeneous: np.ndarray) -> np.ndarray:
    left_h = np.vstack([np.asarray(left, dtype=np.float64), [0.0, 0.0, 1.0]])
    return (left_h @ right_homogeneous)[:2]


def _inverse_warp_coordinates(
    matrix: np.ndarray,
    output_shape: tuple[int, int],
    source_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    matrix = np.asarray(matrix, dtype=np.float64)
    linear = matrix[:, :2]
    translation = matrix[:, 2]
    determinant = float(np.linalg.det(linear))
    if not np.isfinite(matrix).all() or abs(determinant) <= 1e-12:
        raise ValueError("Alignment matrix must be finite and invertible")
    inverse = np.linalg.inv(linear)
    out_h, out_w = output_shape
    rows, cols = np.indices((out_h, out_w), dtype=np.float64)
    destination = np.stack([cols.ravel(), rows.ravel()], axis=0)
    source = inverse @ (destination - translation[:, None])
    source_x = source[0].reshape(out_h, out_w)
    source_y = source[1].reshape(out_h, out_w)
    src_h, src_w = source_shape
    valid = (
        (source_x >= 0.0)
        & (source_x <= src_w - 1.0)
        & (source_y >= 0.0)
        & (source_y <= src_h - 1.0)
    )
    return source_x, source_y, valid


def _bilinear_sample(
    image: np.ndarray,
    source_x: np.ndarray,
    source_y: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    image = np.asarray(image, dtype=np.float64)
    x0 = np.floor(source_x).astype(np.int64)
    y0 = np.floor(source_y).astype(np.int64)
    x0c = np.clip(x0, 0, image.shape[1] - 1)
    y0c = np.clip(y0, 0, image.shape[0] - 1)
    x1c = np.clip(x0 + 1, 0, image.shape[1] - 1)
    y1c = np.clip(y0 + 1, 0, image.shape[0] - 1)
    wx = source_x - x0
    wy = source_y - y0
    sampled = (
        (1.0 - wx) * (1.0 - wy) * image[y0c, x0c]
        + wx * (1.0 - wy) * image[y0c, x1c]
        + (1.0 - wx) * wy * image[y1c, x0c]
        + wx * wy * image[y1c, x1c]
    )
    sampled[~valid] = 0.0
    return sampled.astype(np.float32)


def warp_images_once(
    images: Mapping[str, np.ndarray],
    matrix: np.ndarray,
    *,
    output_shape: tuple[int, int] = (PROCESS_H, PROCESS_W),
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Warp every input image exactly once and return their shared valid mask."""
    if not images:
        raise ValueError("At least one basis image is required")
    shapes = {np.asarray(image).shape for image in images.values()}
    if len(shapes) != 1:
        raise ValueError(f"All basis assets must share one shape, got {shapes}")
    source_shape = next(iter(shapes))
    if len(source_shape) != 2:
        raise ValueError("Basis assets must be 2-D grayscale images")
    source_x, source_y, valid = _inverse_warp_coordinates(matrix, output_shape, source_shape)
    warped = {
        label: _bilinear_sample(image, source_x, source_y, valid)
        for label, image in images.items()
    }
    return warped, _readonly_array(valid, dtype=bool)


def warp_basis_similarity(
    basis: Mapping[str, np.ndarray],
    matrix: np.ndarray,
    *,
    return_mask: bool = False,
) -> dict[str, np.ndarray] | tuple[dict[str, np.ndarray], np.ndarray]:
    """Compatibility wrapper for callers that still hold a plain basis dict."""
    warped, valid = warp_images_once(basis, matrix)
    return (warped, valid) if return_mask else warped


def warp_basis_bundle(
    bundle: BasisBundle,
    candidate_or_matrix: AlignmentCandidate | np.ndarray,
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    matrix = (
        candidate_or_matrix.matrix
        if isinstance(candidate_or_matrix, AlignmentCandidate)
        else candidate_or_matrix
    )
    return warp_images_once(bundle.active_images(), matrix)


def normalized_correlation(
    first: np.ndarray,
    second: np.ndarray,
    valid_mask: np.ndarray,
) -> float:
    mask = np.asarray(valid_mask, dtype=bool)
    a = np.asarray(first, dtype=np.float64)
    b = np.asarray(second, dtype=np.float64)
    if a.shape != b.shape or mask.shape != a.shape:
        raise ValueError("Images and valid mask must have matching shapes")
    mask = mask & np.isfinite(a) & np.isfinite(b)
    if np.count_nonzero(mask) < 3:
        return -1.0
    av = a[mask] - float(a[mask].mean())
    bv = b[mask] - float(b[mask].mean())
    denominator = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denominator <= 1e-12:
        return -1.0
    return float(np.clip(np.dot(av, bv) / denominator, -1.0, 1.0))


def validate_alignment(
    matrix: np.ndarray,
    *,
    scale: float,
    rms_residual_px: float,
    max_residual_px: float,
    valid_coverage: float,
) -> tuple[bool, str]:
    matrix = np.asarray(matrix, dtype=np.float64)
    if matrix.shape != (2, 3) or not np.isfinite(matrix).all():
        return False, "alignment matrix is not finite 2x3"
    if abs(float(np.linalg.det(matrix[:, :2]))) <= 1e-12:
        return False, "alignment matrix is not invertible"
    if not math.isfinite(scale) or not MIN_SCALE <= scale <= MAX_SCALE:
        return False, f"scale {scale:.3f} outside [{MIN_SCALE:.1f}, {MAX_SCALE:.1f}]"
    if not math.isfinite(rms_residual_px) or rms_residual_px > MAX_RMS_RESIDUAL_PX:
        return False, (
            f"RMS residual {rms_residual_px:.2f} px exceeds {MAX_RMS_RESIDUAL_PX:.1f} px"
        )
    if not math.isfinite(max_residual_px) or max_residual_px > MAX_REPROJECTION_PX:
        return False, (
            f"maximum residual {max_residual_px:.2f} px exceeds {MAX_REPROJECTION_PX:.1f} px"
        )
    if (
        not math.isfinite(valid_coverage)
        or not MIN_VALID_COVERAGE <= valid_coverage <= 1.0
    ):
        return False, (
            f"valid coverage {valid_coverage:.1%} outside "
            f"[{MIN_VALID_COVERAGE:.0%}, 100%]"
        )
    return True, ""


def validate_similarity(
    rotation_deg: float,
    scale: float,
    reprojection_px: float,
    *,
    max_reprojection_px: float | None = None,
    coverage: float = 1.0,
    matrix: np.ndarray | None = None,
) -> tuple[bool, str]:
    """Compatibility validator using the complete production thresholds."""
    del rotation_deg  # 180-degree rotations are valid; handedness is explicit.
    matrix_value = np.eye(2, 3, dtype=np.float64) if matrix is None else matrix
    matrix_value = np.asarray(matrix_value, dtype=np.float64)
    if matrix is None:
        matrix_value[:, :2] *= scale
    return validate_alignment(
        matrix_value,
        scale=scale,
        rms_residual_px=reprojection_px,
        max_residual_px=(
            reprojection_px if max_reprojection_px is None else max_reprojection_px
        ),
        valid_coverage=coverage,
    )


def enumerate_alignment_candidates(
    basis_points: LandmarkDetection | np.ndarray,
    live_points: LandmarkDetection | np.ndarray,
    basis_1x1: np.ndarray,
    live_frame: np.ndarray,
) -> tuple[AlignmentCandidate, ...]:
    """Enumerate parity and endpoint hypotheses and rank by valid-mask NCC."""
    canonical = _coerce_points(basis_points, "basis landmarks")
    live = _coerce_points(live_points, "live landmarks")
    basis_reason = _landmark_failure_reason(canonical, image_shape=np.asarray(basis_1x1).shape)
    live_reason = _landmark_failure_reason(live, image_shape=np.asarray(live_frame).shape)
    if basis_reason:
        raise ValueError(f"invalid basis landmarks: {basis_reason}")
    if live_reason:
        raise ValueError(f"invalid live landmarks: {live_reason}")

    candidates: list[AlignmentCandidate] = []
    for parity in ("normal", "mirrored"):
        parity_h = _parity_matrix(parity, width=np.asarray(basis_1x1).shape[1])
        parity_points = project_points(parity_h[:2], canonical)
        for endpoint_order in ("forward", "reversed"):
            destination = live if endpoint_order == "forward" else live[[2, 1, 0]]
            local_matrix, scale, rotation_deg, _, _ = compute_similarity(
                parity_points,
                destination,
            )
            matrix = _compose_affine(local_matrix, parity_h)
            projected = project_points(matrix, canonical)
            residuals = np.linalg.norm(projected - destination, axis=1)
            rms = float(np.sqrt(np.mean(residuals * residuals)))
            maximum = float(np.max(residuals))
            warped, valid_mask = warp_images_once({"1x1": basis_1x1}, matrix)
            coverage = float(np.mean(valid_mask))
            correlation = normalized_correlation(warped["1x1"], live_frame, valid_mask)
            ok, reason = validate_alignment(
                matrix,
                scale=scale,
                rms_residual_px=rms,
                max_residual_px=maximum,
                valid_coverage=coverage,
            )
            candidates.append(
                AlignmentCandidate(
                    matrix=matrix,
                    parity=parity,
                    endpoint_order=endpoint_order,
                    basis_points=canonical,
                    live_points=destination,
                    projected_points=projected,
                    residuals_px=residuals,
                    scale=scale,
                    rotation_deg=rotation_deg,
                    translation_xy=(float(matrix[0, 2]), float(matrix[1, 2])),
                    rms_residual_px=rms,
                    max_residual_px=maximum,
                    valid_coverage=coverage,
                    correlation=correlation,
                    valid=ok,
                    validation_reason=reason,
                )
            )

    # Keep all four named hypotheses even when symmetric 1x1 landmarks make
    # two matrices numerically identical.  Handedness remains a grower choice,
    # and the preview must always permit explicit normal/mirrored review.
    candidates.sort(
        key=lambda candidate: (
            not candidate.valid,
            -candidate.correlation,
            candidate.rms_residual_px,
            candidate.parity != "normal",
            candidate.endpoint_order != "forward",
        )
    )
    return tuple(candidates)


def calibration_is_stale(
    calibration: CalibrationRecord,
    *,
    source_hwnd: int = 0,
    camera_width: int = 0,
    camera_height: int = 0,
    camera_mode: str = "",
    capture_backend: str = "",
    capture_geometry_id: str = "",
    view_segment_id: int | None = None,
    visual_history_generation: int | None = None,
    session_id: str | None = None,
    basis_bundle_id: str | None = None,
    gun_aligned: bool | None = None,
    realignment_active: bool | None = None,
    session_active: bool = True,
) -> tuple[bool, str]:
    """Return whether current acquisition provenance invalidates calibration."""
    if not session_active:
        return True, "session ended"
    if calibration.invalidated_reason:
        return True, calibration.invalidated_reason
    if source_hwnd and source_hwnd != calibration.source_hwnd:
        return True, f"camera HWND changed ({calibration.source_hwnd} -> {source_hwnd})"
    if camera_width and camera_width != calibration.camera_width:
        return True, "camera resolution changed"
    if camera_height and camera_height != calibration.camera_height:
        return True, "camera resolution changed"
    backend = capture_backend or camera_mode
    if backend and backend != calibration.capture_backend:
        return True, f"capture backend changed ({calibration.capture_backend} -> {backend})"
    if (
        capture_geometry_id
        and capture_geometry_id != calibration.capture_geometry_id
    ):
        return True, "capture ROI/chrome geometry changed"
    if view_segment_id is not None and view_segment_id != calibration.view_segment_id:
        return True, "view segment changed"
    if (
        visual_history_generation is not None
        and visual_history_generation != calibration.visual_history_generation
    ):
        return True, "visual-history generation changed"
    if session_id is not None and session_id != calibration.session_id:
        return True, "session changed"
    if basis_bundle_id is not None and basis_bundle_id != calibration.basis_bundle_id:
        return True, "basis bundle changed"
    if gun_aligned is not None and gun_aligned != calibration.gun_aligned:
        return True, "gun-alignment state changed"
    if realignment_active is not None and realignment_active != calibration.realignment_active:
        return True, "realignment state changed"
    return False, ""


__all__ = [
    "ACTIVE_SIMULATOR_LABELS",
    "AlignmentCandidate",
    "BasisAsset",
    "BasisBundle",
    "Calibration",
    "CalibrationRecord",
    "CANONICAL_COORDINATE_FRAME",
    "LandmarkDetection",
    "ORIENTATION_EVIDENCE_KINDS",
    "PROCESS_H",
    "PROCESS_W",
    "PROCESS_WH",
    "RheedFrameSnapshot",
    "calibration_is_stale",
    "compute_similarity",
    "detect_basis_landmarks",
    "detect_landmarks",
    "detect_live_landmarks",
    "enumerate_alignment_candidates",
    "normalize_utc_timestamp",
    "normalized_correlation",
    "project_points",
    "validate_alignment",
    "validate_manual_landmarks",
    "validate_similarity",
    "warp_basis_bundle",
    "warp_basis_similarity",
    "warp_images_once",
]
