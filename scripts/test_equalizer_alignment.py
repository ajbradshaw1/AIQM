"""Unit tests for gui/equalizer_alignment.py."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import sys

# Simple approx helper — avoids approx dependency so tests run
# without a full pytest installation.
def _approx(value, tolerance=1e-9):
    """Return an object whose __eq__ checks approximate equality."""
    class _Approx:
        def __eq__(self, other):
            return abs(float(other) - float(value)) <= tolerance
        def __repr__(self):
            return f"approx({value})"
    return _Approx()

approx = _approx

# Ensure gui/ is importable.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from gui.equalizer_alignment import (
    PROCESS_W,
    PROCESS_H,
    PROCESS_WH,
    FALLBACK_LANDMARKS,
    Calibration,
    detect_basis_landmarks,
    compute_offset,
    validate_offset,
    shift_basis,
    calibration_is_stale,
    _landmarks_are_degenerate,
    _all_same_sign,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gaussian_spot(
    shape: tuple[int, int],
    centre: tuple[float, float],
    sigma: float = 4.0,
    amplitude: float = 200.0,
) -> np.ndarray:
    """Create a 2-D Gaussian spot on a zero background."""
    H, W = shape
    y, x = np.mgrid[0:H, 0:W].astype(np.float64)
    return amplitude * np.exp(
        -((x - centre[0]) ** 2 + (y - centre[1]) ** 2) / (2 * sigma ** 2)
    )


def _synthetic_rheed(spots: list[tuple[float, float]]) -> np.ndarray:
    """Create a synthetic RHEED pattern with Gaussian spots at given positions."""
    img = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float64)
    for cx, cy in spots:
        img += _gaussian_spot((PROCESS_H, PROCESS_W), (cx, cy))
    return img + np.random.default_rng(42).normal(0, 2, (PROCESS_H, PROCESS_W))


def _stale_kwargs(**overrides):
    """Default keyword arguments for calibration_is_stale."""
    defaults = dict(
        source_hwnd=12345,
        camera_width=656,
        camera_height=492,
        camera_mode="screengrab",
        view_segment_id=0,
        session_active=True,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# detect_basis_landmarks
# ---------------------------------------------------------------------------


class TestDetectBasisLandmarks:
    """Tests for detect_basis_landmarks."""

    def test_synthetic(self):
        """Synthetic pattern with 3 known spots — detection within 2 px."""
        # Ground-truth positions (in PROCESS_WH coords).
        gt = [(28.0, 48.0), (64.0, 48.0), (100.0, 48.0)]
        img = _synthetic_rheed(gt)
        landmarks = detect_basis_landmarks(img)

        assert landmarks.shape == (3, 2)
        for i in range(3):
            dist = np.linalg.norm(landmarks[i] - np.array(gt[i]))
            assert dist <= 2.0, f"Landmark {i} off by {dist:.1f} px"

    def test_ordering(self):
        """Leftmost X < specular X < rightmost X."""
        gt = [(28.0, 48.0), (64.0, 48.0), (100.0, 48.0)]
        img = _synthetic_rheed(gt)
        landmarks = detect_basis_landmarks(img)
        xs = landmarks[:, 0]
        assert xs[0] < xs[1] < xs[2]

    def test_fallback(self):
        """Blank image falls back to hardcoded positions without crashing."""
        img = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float64)
        landmarks = detect_basis_landmarks(img)
        assert landmarks.shape == (3, 2)
        # Should equal fallback positions (no real peaks to find).
        np.testing.assert_array_almost_equal(landmarks, FALLBACK_LANDMARKS)


def test_landmarks_are_degenerate_unordered():
    """Wrong X ordering → degenerate."""
    bad = np.array([[70.0, 48.0], [30.0, 48.0], [90.0, 48.0]])
    assert _landmarks_are_degenerate(bad) is True


def test_landmarks_are_degenerate_too_close():
    """Peaks too close together → degenerate."""
    bad = np.array([[28.0, 48.0], [30.0, 48.0], [100.0, 48.0]])
    assert _landmarks_are_degenerate(bad) is True


def test_landmarks_are_degenerate_out_of_bounds():
    """Y out of bounds → degenerate."""
    bad = np.array([[28.0, -1.0], [64.0, 48.0], [100.0, 48.0]])
    assert _landmarks_are_degenerate(bad) is True


# ---------------------------------------------------------------------------
# compute_offset
# ---------------------------------------------------------------------------


class TestComputeOffset:
    """Tests for compute_offset."""

    def test_identity(self):
        """Same basis and live points → offset ≈ (0,0)."""
        pts = np.array([[28.0, 48.0], [64.0, 48.0], [100.0, 48.0]])
        (dx, dy), offsets, md = compute_offset(pts, pts)
        assert dx == approx(0.0, tolerance=1e-9)
        assert dy == approx(0.0, tolerance=1e-9)
        assert md == approx(0.0, tolerance=1e-9)

    def test_known_shift(self):
        """All 3 points shifted by (+5, -3) → offset = (+5, -3)."""
        basis = np.array([[28.0, 48.0], [64.0, 48.0], [100.0, 48.0]])
        live = basis + np.array([[5.0, -3.0]])
        (dx, dy), offsets, md = compute_offset(basis, live)
        assert dx == approx(5.0)
        assert dy == approx(-3.0)
        assert md == approx(0.0, tolerance=1e-9)

    def test_disagreement(self):
        """One point offset differs → max_disagreement > 0, mean still correct."""
        basis = np.array([[28.0, 48.0], [64.0, 48.0], [100.0, 48.0]])
        # Points 0,1 shifted by (5, 0), point 2 shifted by (7, 0).
        live = basis + np.array([[5.0, 0.0], [5.0, 0.0], [7.0, 0.0]])
        (dx, dy), offsets, md = compute_offset(basis, live)
        # Mean dx = (5+5+7)/3 ≈ 5.667
        assert dx == approx((5 + 5 + 7) / 3)
        assert dy == approx(0.0)
        assert md > 0.0


# ---------------------------------------------------------------------------
# validate_offset
# ---------------------------------------------------------------------------


class TestValidateOffset:
    """Tests for validate_offset."""

    def test_good(self):
        """max_disagreement < 5, same sign → valid."""
        offsets = np.array([[5.0, -2.0], [4.0, -3.0], [6.0, -1.0]])
        ok, reason = validate_offset(offsets, max_disagreement=2.5)
        assert ok is True
        assert reason == ""

    def test_high_disagreement(self):
        """max_disagreement > 5 → invalid."""
        offsets = np.array([[5.0, 0.0], [5.0, 0.0], [15.0, 0.0]])
        ok, reason = validate_offset(offsets, max_disagreement=10.0)
        assert ok is False
        assert "disagree" in reason.lower()

    def test_opposite_signs(self):
        """One offset has opposite X sign → invalid."""
        offsets = np.array([[5.0, 0.0], [-3.0, 0.0], [5.0, 0.0]])
        # All magnitudes small, but sign mismatch.
        ok, reason = validate_offset(offsets, max_disagreement=1.0)
        assert ok is False
        assert "direction" in reason.lower()

    def test_large_shift(self):
        """Offset magnitude > MAX_OFFSET_MAGNITUDE → invalid."""
        big = np.array([[50.0, 0.0], [50.0, 0.0], [50.0, 0.0]])
        ok, reason = validate_offset(big, max_disagreement=0.0)
        assert ok is False
        assert "off-screen" in reason.lower()


def test_all_same_sign():
    assert _all_same_sign(np.array([1.0, 2.0, 3.0])) is True
    assert _all_same_sign(np.array([-1.0, -2.0, 0.0])) is True  # zero ignored
    assert _all_same_sign(np.array([1.0, -1.0, 0.0])) is False


# ---------------------------------------------------------------------------
# shift_basis
# ---------------------------------------------------------------------------


class TestShiftBasis:
    """Tests for shift_basis."""

    def test_known(self):
        """Shift by (+3, 0): pixel moves right by 3 columns."""
        img = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
        img[48, 64] = 255.0  # bright pixel at specular in basis
        basis = {"1x1": img}
        shifted = shift_basis(basis, dx=3.0, dy=0.0)
        # The bright pixel should now be at column 67.
        assert shifted["1x1"][48, 67] == approx(255.0)
        # Original position should be zeroed (wrapped region).
        assert shifted["1x1"][48, 0] == approx(0.0)
        assert shifted["1x1"][48, 1] == approx(0.0)
        assert shifted["1x1"][48, 2] == approx(0.0)

    def test_shape(self):
        """Output dict has same keys and shapes."""
        basis = {
            "1x1": np.ones((PROCESS_H, PROCESS_W), dtype=np.float32),
            "c(6x2)": np.ones((PROCESS_H, PROCESS_W), dtype=np.float32) * 2,
        }
        shifted = shift_basis(basis, dx=0.0, dy=0.0)
        assert set(shifted.keys()) == set(basis.keys())
        for k in basis:
            assert shifted[k].shape == basis[k].shape

    def test_no_mutation(self):
        """Input dict is unchanged after call."""
        img = np.random.default_rng(1).random((PROCESS_H, PROCESS_W)).astype(np.float32)
        basis = {"1x1": img.copy()}
        _ = shift_basis(basis, dx=10.0, dy=-5.0)
        np.testing.assert_array_equal(basis["1x1"], img)


# ---------------------------------------------------------------------------
# calibration_is_stale
# ---------------------------------------------------------------------------


class TestCalibrationIsStale:
    """Tests for calibration_is_stale."""

    def _cal(self, **overrides):
        return Calibration(
            basis_points=np.zeros((3, 2)),
            live_points=np.zeros((3, 2)),
            offset=(0.0, 0.0),
            per_point_offsets=np.zeros((3, 2)),
            max_disagreement_px=0.0,
            source_hwnd=12345,
            camera_width=656,
            camera_height=492,
            camera_mode="screengrab",
            view_segment_id=0,
            **overrides,
        )

    def test_fresh(self):
        """Same provenance → not stale."""
        cal = self._cal()
        stale, _ = calibration_is_stale(cal, **_stale_kwargs())
        assert stale is False

    def test_hwnd(self):
        """Different source_hwnd → stale."""
        cal = self._cal()
        stale, reason = calibration_is_stale(
            cal, **_stale_kwargs(source_hwnd=99999),
        )
        assert stale is True
        assert "hwnd" in reason.lower()

    def test_width(self):
        """Different camera width → stale."""
        cal = self._cal()
        stale, reason = calibration_is_stale(
            cal, **_stale_kwargs(camera_width=800),
        )
        assert stale is True
        assert "resolution" in reason.lower()

    def test_height(self):
        """Different camera height → stale."""
        cal = self._cal()
        stale, reason = calibration_is_stale(
            cal, **_stale_kwargs(camera_height=600),
        )
        assert stale is True

    def test_mode(self):
        """Different camera mode → stale."""
        cal = self._cal()
        stale, reason = calibration_is_stale(
            cal, **_stale_kwargs(camera_mode="direct"),
        )
        assert stale is True
        assert "mode" in reason.lower()

    def test_segment(self):
        """Different view_segment_id → stale."""
        cal = self._cal()
        stale, reason = calibration_is_stale(
            cal, **_stale_kwargs(view_segment_id=3),
        )
        assert stale is True
        assert "segment" in reason.lower()

    def test_session_inactive(self):
        """Session not active → stale."""
        cal = self._cal()
        stale, _ = calibration_is_stale(
            cal, **_stale_kwargs(session_active=False),
        )
        assert stale is True

    def test_none_optional(self):
        """None view_segment_id → not stale (graceful)."""
        cal = self._cal()
        stale, _ = calibration_is_stale(
            cal, **_stale_kwargs(view_segment_id=None),
        )
        assert stale is False


# ---------------------------------------------------------------------------
# Calibration dataclass
# ---------------------------------------------------------------------------


def test_calibration_defaults():
    """Calibration can be constructed with minimal fields."""
    cal = Calibration(
        basis_points=np.array([[1.0, 2.0]] * 3),
        live_points=np.array([[3.0, 4.0]] * 3),
        offset=(2.0, 2.0),
        per_point_offsets=np.array([[2.0, 2.0]] * 3),
        max_disagreement_px=0.0,
    )
    assert cal.grower_accepted is False
    assert cal.offset == (2.0, 2.0)
    assert cal.created_at_utc  # auto-filled
