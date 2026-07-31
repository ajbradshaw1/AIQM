"""
Equalizer camera alignment — similarity registration via 1x1 landmarks.

The three integer-order 1x1 diffraction spots (left first-order, specular,
right first-order) form a rigid triangle visible in every reconstruction.
Auto-detected in both the basis and live images, they provide 3 point
correspondences for a similarity transform (rotation + translation +
uniform scale) computed via Umeyama's algorithm.

When the gun geometry changes, the RHEED pattern can translate AND rotate
on the phosphor screen. The similarity transform corrects both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

try:
    from scipy.ndimage import gaussian_filter1d, affine_transform
except ImportError:
    gaussian_filter1d = None  # type: ignore
    affine_transform = None  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESS_W = 128
PROCESS_H = 96

# Landmark detection.
GAUSSIAN_SIGMA = 6.0
STRIP_HALF_WIDTH = 12
MIN_PEAK_SEPARATION = 8

# Validation thresholds.
MAX_REPROJECTION_PX = 5.0
MAX_ROTATION_DEG = 90.0
MIN_SCALE = 0.2
MAX_SCALE = 5.0

FALLBACK_LANDMARKS = np.array([
    [PROCESS_W * 0.22, PROCESS_H * 0.50],
    [PROCESS_W * 0.50, PROCESS_H * 0.50],
    [PROCESS_W * 0.78, PROCESS_H * 0.50],
], dtype=np.float64)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Calibration:
    """An accepted similarity alignment (rotation + translation + scale)."""
    basis_points: np.ndarray       # (3, 2) auto-detected basis landmarks
    live_points: np.ndarray        # (3, 2) auto-detected live landmarks
    matrix: np.ndarray             # (2, 3) similarity: basis_coords → live_coords
    rotation_deg: float            # rotation angle in degrees
    scale: float                   # uniform scale factor
    reprojection_px: float         # mean reprojection error
    # Provenance.
    created_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_hwnd: int = 0
    camera_width: int = 0
    camera_height: int = 0
    camera_mode: str = ""
    view_segment_id: int = 0
    grower_accepted: bool = False


# ---------------------------------------------------------------------------
# Peak detection helpers
# ---------------------------------------------------------------------------

def _find_local_maxima(signal: np.ndarray, min_separation: int = 8) -> np.ndarray:
    """Return indices of local maxima in a 1-D signal, greedily merged."""
    n = len(signal)
    is_max = np.zeros(n, dtype=bool)
    is_max[1:-1] = (signal[1:-1] > signal[:-2]) & (signal[1:-1] > signal[2:])
    indices = np.where(is_max)[0]
    if len(indices) <= 1:
        return indices
    order = np.argsort(-signal[indices])
    kept: list[int] = []
    for idx in indices[order]:
        if all(abs(int(idx) - int(k)) >= min_separation for k in kept):
            kept.append(int(idx))
    return np.array(sorted(kept))


def _landmarks_are_degenerate(landmarks: np.ndarray) -> bool:
    xs = landmarks[:, 0]; ys = landmarks[:, 1]
    H, W = PROCESS_H, PROCESS_W
    if not (xs[0] < xs[1] < xs[2]):
        return True
    if (xs[1] - xs[0]) < MIN_PEAK_SEPARATION or (xs[2] - xs[1]) < MIN_PEAK_SEPARATION:
        return True
    for y in ys:
        if y <= 0 or y >= H - 1:
            return True
    return False


# ---------------------------------------------------------------------------
# Landmark detection (basis and live)
# ---------------------------------------------------------------------------

def detect_basis_landmarks(basis_1x1: np.ndarray) -> np.ndarray:
    """Auto-detect the 3 integer-order 1x1 spots in a basis image at PROCESS_WH.

    Excludes image edges (5 % margin) so bright image-boundary pixels
    don't dominate the column-sum and row-sum profiles.
    """
    if basis_1x1.ndim != 2:
        raise ValueError(f"Expected 2-D, got shape {basis_1x1.shape}")
    if gaussian_filter1d is None:
        return FALLBACK_LANDMARKS.copy()

    f = basis_1x1.astype(np.float64)
    H, W = f.shape

    margin_x = max(8, int(W * 0.05))
    margin_y = max(6, int(H * 0.05))

    # Column-sum over the interior (exclude top/bottom edge rows).
    col_sum = f[margin_y:H - margin_y, margin_x:W - margin_x].sum(axis=0)
    col_smooth = gaussian_filter1d(col_sum, sigma=GAUSSIAN_SIGMA)

    peak_indices = _find_local_maxima(col_smooth, min_separation=MIN_PEAK_SEPARATION)
    if len(peak_indices) < 3:
        return FALLBACK_LANDMARKS.copy()

    peak_values = col_smooth[peak_indices]
    top3_idx = np.argpartition(peak_values, -3)[-3:]
    top3_x = sorted(int(peak_indices[i]) + margin_x for i in top3_idx)
    lx, sx, rx = top3_x[0], top3_x[1], top3_x[2]

    def _find_y(x: int) -> float:
        lo, hi = max(0, x - STRIP_HALF_WIDTH), min(W, x + STRIP_HALF_WIDTH + 1)
        strip = f[margin_y:H - margin_y, lo:hi].sum(axis=1)
        strip_smooth = gaussian_filter1d(strip, sigma=GAUSSIAN_SIGMA)
        return float(strip_smooth.argmax() + margin_y)

    landmarks = np.array(
        [[lx, _find_y(lx)], [sx, _find_y(sx)], [rx, _find_y(rx)]],
        dtype=np.float64,
    )
    if _landmarks_are_degenerate(landmarks):
        return FALLBACK_LANDMARKS.copy()
    return landmarks


def detect_live_landmarks(live_frame: np.ndarray) -> np.ndarray | None:
    """Auto-detect the 3 integer-order 1x1 spots in a live frame.

    First tries column-sum (same as basis), then falls back to 2-D
    peak detection.  Returns None when no method finds 3 well-separated
    spots, signalling that manual clicking should be used instead.
    """
    if live_frame.ndim != 2:
        return None

    # --- Method 1: column-sum (same as basis) ---
    try:
        result = detect_basis_landmarks(live_frame)
        if not np.allclose(result, FALLBACK_LANDMARKS, rtol=1e-5, atol=1e-3):
            return result
    except Exception:
        pass

    # --- Method 2: 2-D local-maxima detection ---
    try:
        spots = _detect_spots_2d(live_frame)
        if spots is not None:
            return spots
    except Exception:
        pass

    return None


def _detect_spots_2d(image: np.ndarray) -> np.ndarray | None:
    """Find the 3 brightest horizontally-arranged 2-D spots.

    Gaussian-smooths the image, finds local maxima, and returns the
    3 brightest that are well-separated and not touching image edges.
    """
    from scipy.ndimage import maximum_filter

    f = image.astype(np.float64)
    H, W = f.shape

    margin_x = max(8, int(W * 0.05))
    margin_y = max(6, int(H * 0.05))

    # Smooth and find local maxima.
    smooth = gaussian_filter1d(
        gaussian_filter1d(f, sigma=3.0, axis=0),
        sigma=3.0, axis=1,
    )
    # A pixel is a local max if it equals the max in its 5×5 neighbourhood.
    footprint = np.ones((5, 5), dtype=bool)
    local_max = smooth == maximum_filter(smooth, footprint=footprint)
    # Exclude edges.
    local_max[:margin_y, :] = False
    local_max[-margin_y:, :] = False
    local_max[:, :margin_x] = False
    local_max[:, -margin_x:] = False

    ys, xs = np.where(local_max)
    if len(ys) < 3:
        return None

    # Sort by smoothed intensity, take top candidates.
    intensities = smooth[ys, xs]
    order = np.argsort(-intensities)
    candidates = list(zip(xs[order], ys[order], intensities[order]))

    # Greedy selection: take brightest first, enforce minimum separation.
    MIN_SEP = MIN_PEAK_SEPARATION
    chosen: list[tuple[float, float]] = []
    for cx, cy, _ in candidates:
        if all(abs(cx - px) >= MIN_SEP for px, _ in chosen):
            chosen.append((float(cx), float(cy)))
        if len(chosen) == 3:
            break

    if len(chosen) < 3:
        return None

    # Sort left-to-right.
    chosen.sort(key=lambda p: p[0])
    spots = np.array(chosen, dtype=np.float64)

    if _landmarks_are_degenerate(spots):
        return None
    return spots


# ---------------------------------------------------------------------------
# Similarity transform (Umeyama)
# ---------------------------------------------------------------------------

def compute_similarity(
    src: np.ndarray, dst: np.ndarray,
) -> tuple[np.ndarray, float, float, float, float]:
    """Compute 2-D similarity transform mapping src → dst via Umeyama's algorithm.

    Args:
        src: (N, 2) source points (basis landmarks).
        dst: (N, 2) destination points (live landmarks).

    Returns:
        ``(matrix, scale, rotation_deg, dx, dy)`` where *matrix* is a
        2×3 affine matrix mapping homogeneous src coords to dst coords.
    """
    N = src.shape[0]
    mu_src = src.mean(axis=0)
    mu_dst = dst.mean(axis=0)

    src_c = src - mu_src
    dst_c = dst - mu_dst

    var_src = np.sum(src_c ** 2) / N
    if var_src < 1e-12:
        # Degenerate: return identity with translation only.
        matrix = np.array([[1.0, 0.0, mu_dst[0] - mu_src[0]],
                           [0.0, 1.0, mu_dst[1] - mu_src[1]]], dtype=np.float64)
        return matrix, 1.0, 0.0, mu_dst[0] - mu_src[0], mu_dst[1] - mu_src[1]

    H = (src_c.T @ dst_c) / N   # 2×2 cross-covariance
    U, S, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T              # 2×2 rotation
    if np.linalg.det(R) < 0:
        Vt[-1, :] *= -1
        R = Vt.T @ U.T

    scale = np.trace(np.diag(S) @ Vt @ U.T) / (var_src * N) * N
    # Simpler: scale = (singular value ratio proxy)
    scale = float(np.sum(dst_c ** 2) / max(np.sum(src_c ** 2), 1e-12)) ** 0.5
    scale = max(0.1, min(10.0, scale))

    A = scale * R                    # 2×2 scaled rotation
    t = mu_dst - A @ mu_src          # translation

    matrix = np.hstack([A, t.reshape(2, 1)])

    rotation_deg = float(np.arctan2(R[1, 0], R[0, 0]) * 180.0 / np.pi)

    # Reprojection error.
    src_h = np.hstack([src, np.ones((N, 1))])
    projected = (matrix @ src_h.T).T
    reproj = float(np.mean(np.linalg.norm(projected - dst, axis=1)))

    return matrix, scale, rotation_deg, float(t[0]), float(t[1])


# ---------------------------------------------------------------------------
# Warp
# ---------------------------------------------------------------------------

def warp_basis_similarity(
    basis: dict[str, np.ndarray], matrix: np.ndarray,
) -> dict[str, np.ndarray]:
    """Warp each basis image through a 2×3 similarity matrix.

    Direct affine_transform at PROCESS_WH. No padding, no crop/resize.
    Bilinear interpolation (order=1) to avoid cubic ringing artifacts.
    """
    if affine_transform is None:
        raise ImportError("scipy.ndimage.affine_transform required")

    H, W = PROCESS_H, PROCESS_W
    A = matrix[:, :2]
    t = matrix[:, 2]

    # Forward (xy):  live = A @ basis + t
    # Inverse (xy):  basis = A_inv @ live + (-A_inv @ t)
    A_inv = np.linalg.inv(A)
    off_xy = -A_inv @ t

    # scipy.ndimage.affine_transform uses (row, col) = (y, x) ordering.
    # A_inv = [[p, q],        M_for_affine = [[s, r],
    #          [r, s]]  (xy)                  [q, p]]  (row,col)
    p, q = A_inv[0, 0], A_inv[0, 1]
    r, s = A_inv[1, 0], A_inv[1, 1]
    M = np.array([[s, r], [q, p]], dtype=np.float64)

    # Offset also needs (y, x) order: off_xy = (dx, dy) -> offset = (dy, dx).
    offset = np.array([off_xy[1], off_xy[0]])

    warped: dict[str, np.ndarray] = {}
    for label, img in basis.items():
        warped[label] = affine_transform(
            img.astype(np.float64), M, offset=offset,
            output_shape=(H, W), order=1, mode='constant', cval=0.0,
        ).astype(np.float32)

    return warped


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_similarity(
    rotation_deg: float, scale: float, reprojection_px: float,
) -> tuple[bool, str]:
    """Validate a computed similarity transform."""
    if reprojection_px > MAX_REPROJECTION_PX:
        return False, f"Reprojection error {reprojection_px:.1f} px exceeds {MAX_REPROJECTION_PX:.0f} px"
    if abs(rotation_deg) > MAX_ROTATION_DEG:
        return False, f"Rotation {rotation_deg:.1f}° exceeds ±{MAX_ROTATION_DEG:.0f}°"
    if scale < MIN_SCALE or scale > MAX_SCALE:
        return False, f"Scale {scale:.2f} outside [{MIN_SCALE}, {MAX_SCALE}]"
    return True, ""


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def calibration_is_stale(
    cal: Calibration,
    *,
    source_hwnd: int = 0,
    camera_width: int = 0,
    camera_height: int = 0,
    camera_mode: str = "",
    view_segment_id: int | None = None,
    session_active: bool = True,
) -> tuple[bool, str]:
    """Check whether a calibration is still valid."""
    if not session_active:
        return True, "Session ended"
    if source_hwnd and source_hwnd != cal.source_hwnd:
        return True, f"Camera HWND changed ({cal.source_hwnd} → {source_hwnd})"
    if camera_width and camera_width != cal.camera_width:
        return True, f"Resolution changed ({cal.camera_width}×{cal.camera_height} → {camera_width}×{camera_height})"
    if camera_height and camera_height != cal.camera_height:
        return True, f"Resolution changed ({cal.camera_width}×{cal.camera_height} → {camera_width}×{camera_height})"
    if camera_mode and camera_mode != cal.camera_mode:
        return True, f"Camera mode changed ({cal.camera_mode} → {camera_mode})"
    if view_segment_id is not None and view_segment_id != cal.view_segment_id:
        return True, f"View segment changed ({cal.view_segment_id} → {view_segment_id})"
    return False, ""
