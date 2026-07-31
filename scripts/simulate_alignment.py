"""
Simulate Equalizer alignment with similarity transform.

Uses KNOWN point correspondences (bypassing detection) to verify the full
pipeline: Umeyama → warp → reconstruction → MSE comparison.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from gui.equalizer_alignment import (
    compute_similarity, validate_similarity, warp_basis_similarity,
    PROCESS_W, PROCESS_H,
)
from scripts.equalizer_ui import load_class_means, PROCESS_WH, apply_green_palette

OUT_DIR = _REPO / "tmp" / "alignment_simulation"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    print("=" * 60)
    print("Equalizer Alignment — Similarity Transform Pipeline Test")
    print("=" * 60)

    # 1. Load basis.
    print("\n[1] Loading basis images...")
    basis = load_class_means(PROCESS_WH)
    print(f"    Loaded: {list(basis.keys())}")
    basis_1x1 = basis["1x1"]

    # 2. Known basis landmarks (auto-detected from 1x1 basis).
    #    These are the 3 integer-order 1x1 spots.
    from gui.equalizer_alignment import detect_basis_landmarks
    basis_pts = detect_basis_landmarks(basis_1x1)
    print(f"\n[2] Basis landmarks:\n{basis_pts}")

    # 3. Create rotated+shifted live frame via proper affine warp.
    TRUE_ROT = 5.0   # degrees
    TRUE_DX, TRUE_DY = 9.0, -5.0
    print(f"\n[3] Creating live frame: rot={TRUE_ROT}°, dx={TRUE_DX:.0f}, dy={TRUE_DY:.0f}")

    theta = np.radians(TRUE_ROT)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    R = np.array([[cos_t, -sin_t], [sin_t, cos_t]])
    t = np.array([TRUE_DX, TRUE_DY])

    # Compute expected live positions of the basis landmarks.
    live_expected = (R @ basis_pts.T).T + t
    print(f"    Expected live positions:\n{live_expected}")

    # Build forward similarity matrix and use warp_basis_similarity to
    # create the live frame. This ensures the same coordinate convention
    # as the correction path.
    A_true = np.array([[cos_t, -sin_t], [sin_t, cos_t]])  # R in (x,y)
    forward_matrix = np.hstack([A_true, t.reshape(2, 1)])
    warped_dict = warp_basis_similarity({"live": basis_1x1}, forward_matrix)
    live_frame = warped_dict["live"].astype(np.float32)

    rng = np.random.default_rng(42)
    live_frame += rng.normal(0, 3, live_frame.shape).astype(np.float32)
    live_frame = np.clip(live_frame, 0, 255)

    # Verify: check where the specular spot ended up.
    max_pos = np.unravel_index(live_frame.argmax(), live_frame.shape)
    print(f"    Live max at (row={max_pos[0]}, col={max_pos[1]})")
    print(f"    Expected specular at (row≈{live_expected[1,1]:.0f}, col≈{live_expected[1,0]:.0f})")

    # 4. Use KNOWN point pairs (bypassing detection).
    print(f"\n[4] Computing similarity transform from known point pairs...")
    matrix, scale, rot_deg, dx, dy = compute_similarity(basis_pts, live_expected)

    N = basis_pts.shape[0]
    src_h = np.hstack([basis_pts, np.ones((N, 1))])
    projected = (matrix @ src_h.T).T
    reproj = float(np.mean(np.linalg.norm(projected - live_expected, axis=1)))
    ok, reason = validate_similarity(rot_deg, scale, reproj)

    print(f"    Rotation: {rot_deg:.3f}° (true={TRUE_ROT}°)")
    print(f"    Scale:    {scale:.4f} (true=1.0)")
    print(f"    dx, dy:   ({dx:.2f}, {dy:.2f}) (true=({TRUE_DX}, {TRUE_DY}))")
    print(f"    Reprojection: {reproj:.4f} px")
    print(f"    Valid:    {'PASS' if ok else 'FAIL'} — {reason}")

    # 5. Warp and compare.
    print(f"\n[5] Warping basis and comparing reconstructions...")
    warped = warp_basis_similarity(basis, matrix)

    weights = {"1x1": 0.90, "Tw(2x1)": 0.04, "c(6x2)": 0.03, "RT13": 0.02, "HTR": 0.01}

    uncal_recon = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
    for label, w in weights.items():
        if label in basis:
            uncal_recon += w * basis[label]

    cal_recon = np.zeros((PROCESS_H, PROCESS_W), dtype=np.float32)
    for label, w in weights.items():
        if label in warped:
            cal_recon += w * warped[label]

    uncal_mse = float(np.mean((live_frame - uncal_recon) ** 2))
    cal_mse = float(np.mean((live_frame - cal_recon) ** 2))
    print(f"    Uncalibrated MSE: {uncal_mse:.1f}")
    print(f"    Calibrated MSE:   {cal_mse:.1f}")
    print(f"    Improvement:      {uncal_mse / max(cal_mse, 0.001):.1f}x")

    # 6. Save.
    def to_u8(a): return np.clip(a, 0, 255).astype(np.uint8)
    panels = [apply_green_palette(to_u8(a)) for a in [live_frame, uncal_recon, cal_recon]]
    Image.fromarray(np.hstack(panels)).save(str(OUT_DIR / "comparison.png"))

    diff_h = np.hstack([
        apply_green_palette(to_u8(np.abs(live_frame - uncal_recon) * 5)),
        apply_green_palette(to_u8(np.abs(live_frame - cal_recon) * 5)),
    ])
    Image.fromarray(diff_h).save(str(OUT_DIR / "difference.png"))

    print(f"\n    → {OUT_DIR / 'comparison.png'}")
    print(f"    → {OUT_DIR / 'difference.png'}")
    print(f"    Layout: [Live] | [Uncalibrated] | [Calibrated]")

    # 7. Check.
    ok_rot = abs(rot_deg - TRUE_ROT) < 1.0
    ok_dx = abs(dx - TRUE_DX) < 2.0
    ok_dy = abs(dy - TRUE_DY) < 2.0
    print(f"\n[7] Final: rot={'OK' if ok_rot else 'FAIL'}, "
          f"dx={'OK' if ok_dx else 'FAIL'}, dy={'OK' if ok_dy else 'FAIL'}")
    print(f"    MSE improvement: {uncal_mse/cal_mse:.1f}x — {'GOOD' if uncal_mse/cal_mse > 10 else 'POOR'}")
    print("Done.")


if __name__ == "__main__":
    main()
