"""Offline smoke validation for the Equalizer alignment pipeline.

The script returns a non-zero exit code on every failed gate so it can be used
in workstation validation and CI.  Images are diagnostic artifacts only; test
decisions use numeric geometry, coverage, and valid-mask correlation.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from gui.equalizer_alignment import (
    PROCESS_H,
    PROCESS_W,
    detect_basis_landmarks,
    enumerate_alignment_candidates,
    project_points,
    warp_basis_bundle,
    warp_basis_similarity,
)
from scripts.equalizer_ui import apply_green_palette, load_basis_bundle


OUT_DIR = _REPO / "tmp" / "alignment_simulation"


def _known_matrix(*, mirrored: bool = False) -> np.ndarray:
    angle = math.radians(5.0)
    scale = 0.96
    linear = scale * np.array(
        [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]],
        dtype=np.float64,
    )
    local = np.column_stack([linear, [8.0, -3.0]])
    if not mirrored:
        return local
    parity = np.array(
        [[-1.0, 0.0, PROCESS_W - 1.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    return (np.vstack([local, [0.0, 0.0, 1.0]]) @ parity)[:2]


def _matching_candidate(candidates, expected: np.ndarray):
    return min(candidates, key=lambda candidate: float(np.max(np.abs(candidate.matrix - expected))))


def _to_rgb(image: np.ndarray) -> np.ndarray:
    return apply_green_palette(np.clip(image, 0, 255).astype(np.uint8))


def _run_case(bundle, basis_points: np.ndarray, *, mirrored: bool) -> tuple[bool, str, np.ndarray]:
    expected = _known_matrix(mirrored=mirrored)
    basis_1x1 = bundle.asset("1x1").image
    assert basis_1x1 is not None
    live = warp_basis_similarity({"1x1": basis_1x1}, expected)["1x1"]
    live_points = project_points(expected, basis_points)
    # The detector/manual contract is observed left-centre-right order.  A
    # mirrored camera reverses the canonical endpoint identities, so emulate
    # detector output rather than feeding transformed canonical order back in.
    live_points = live_points[np.argsort(live_points[:, 0])]
    candidates = enumerate_alignment_candidates(basis_points, live_points, basis_1x1, live)
    if not candidates:
        return False, "no candidates generated", live

    matched = _matching_candidate(candidates, expected)
    matrix_error = float(np.max(np.abs(matched.matrix - expected)))
    warped, valid_mask = warp_basis_bundle(bundle, matched)
    calibrated = warped["1x1"]
    valid = np.asarray(valid_mask, dtype=bool)
    calibrated_mse = float(np.mean((live[valid] - calibrated[valid]) ** 2))
    uncalibrated_mse = float(np.mean((live[valid] - basis_1x1[valid]) ** 2))
    improvement = uncalibrated_mse / max(calibrated_mse, 1e-12)

    checks = {
        "candidate valid": matched.valid,
        "matrix error <= 1e-6": matrix_error <= 1e-6,
        "RMS residual <= 3 px": matched.rms_residual_px <= 3.0,
        "max residual <= 5 px": matched.max_residual_px <= 5.0,
        "coverage >= 50%": matched.valid_coverage >= 0.5,
        "correlation >= 0.999": matched.correlation >= 0.999,
        "MSE improved": improvement > 2.0,
        "parity preserved": matched.parity == ("mirrored" if mirrored else "normal"),
    }
    details = (
        f"parity={matched.parity}, correlation={matched.correlation:.6f}, "
        f"coverage={matched.valid_coverage:.1%}, matrix_error={matrix_error:.3g}, "
        f"MSE improvement={improvement:.1f}x"
    )
    failures = [name for name, passed in checks.items() if not passed]
    panel = np.hstack([_to_rgb(live), _to_rgb(basis_1x1), _to_rgb(calibrated)])
    if failures:
        return False, f"{details}; failed: {', '.join(failures)}", panel
    return True, details, panel


def main() -> int:
    try:
        bundle = load_basis_bundle()
        detection = detect_basis_landmarks(bundle.asset("1x1").image)
        if not detection.success:
            print(f"FAIL: canonical 1x1 landmark detection: {detection.reason}")
            return 1

        results = []
        panels = []
        for mirrored in (False, True):
            passed, details, panel = _run_case(
                bundle,
                detection.points,
                mirrored=mirrored,
            )
            label = "mirrored" if mirrored else "normal"
            print(f"{'PASS' if passed else 'FAIL'} [{label}] {details}")
            results.append(passed)
            panels.append(panel)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = OUT_DIR / "comparison.png"
        Image.fromarray(np.vstack(panels)).save(output_path)
        print(f"diagnostic={output_path}")
        print("layout per row: live | uncalibrated canonical | calibrated")

        if not all(results):
            print("FAIL: Equalizer alignment simulation did not pass every gate")
            return 1
        print(f"PASS: bundle={bundle.bundle_id}")
        return 0
    except Exception as exc:
        print(f"FAIL: unexpected alignment simulation error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
