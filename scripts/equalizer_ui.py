#!/usr/bin/env python3
"""Shared Equalizer basis, fitting, palette, and legacy preview helpers.

The four active canonical simulator classes are 1x1, Tw(2x1), c(6x2), and
RT13; HTR remains unavailable. Production labeling is deliberately confined
to Growth Monitor, where an app-owned camera calibration, RHEED QC state, and
capture provenance are mandatory. Direct execution exits nonzero and the
legacy window cannot save labels.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gui.equalizer_alignment import (
    ACTIVE_SIMULATOR_LABELS,
    BasisAsset,
    BasisBundle,
    PROCESS_WH as CANONICAL_PROCESS_WH,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPushButton, QSlider, QStatusBar,
    QVBoxLayout, QWidget,
)

CLASS_LABELS = [*ACTIVE_SIMULATOR_LABELS, "HTR"]
ACTIVE_CLASS_LABELS = list(ACTIVE_SIMULATOR_LABELS)
INACTIVE_CLASS_LABELS = [
    label for label in CLASS_LABELS if label not in ACTIVE_CLASS_LABELS
]
IMAGE_EXTS = {".png", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff"}

# Legacy cache-builder inputs retained for scripts/build_equalizer_cache.py.
# The runtime loader below intentionally does not consume this empirical cache.
CLASSIFIER2_DATA_ROOT = Path(
    os.environ.get(
        "AIQM_CLASSIFIER_DATA",
        "/Users/aj/Documents/Research/Data/RHEED image classifier training data",
    )
)
CLASS_DIRS: dict[str, str] = {
    "1x1": "STO_ideal_1x1",
    "Tw(2x1)": "STO_ideal_Twinned2x1",
    "c(6x2)": "STO_ideal_c6x2",
    "RT13": "STO_ideal_RT13",
    "HTR": "STO_ideal_HTR",
}

# Internal processing resolution — matches the SVD prototype so basis
# vectors are pixel-comparable. Display upscales for visibility.
PROCESS_WH = (128, 96)
DISPLAY_WH = (520, 390)
MEANS_CACHE = _REPO_ROOT / "data" / "equalizer_class_means.npz"
STANDALONE_DISABLED_MESSAGE = (
    "Standalone Equalizer labeling is disabled because it cannot prove camera "
    "alignment, RHEED QC, or capture provenance. Use the Growth Monitor Live "
    "Equalizer or Events workflow."
)
SAFE_KEYS = {
    label: label.replace("(", "_").replace(")", "_").replace(" ", "")
    for label in CLASS_LABELS
}

# Physics-simulated basis from Liu et al. (J. Vac. Sci. Technol. B 2022),
# rendered from the parameters in their xlsx, exported via the PI's earlier
# presentation. Used for the four well-defined reconstructions. HTR has no
# canonical simulator-frame asset in v1 and is therefore unavailable.
SIMULATED_BASIS_DIR = Path(__file__).parent.parent / "data" / "simulated_basis"
SIMULATED_BASIS_FILES = {
    "1x1": "1x1.png",
    "Tw(2x1)": "Tw_2x1.png",
    "c(6x2)": "c_6x2.png",
    "RT13": "rt13.png",
    # HTR intentionally omitted — canonical simulator basis pending.
}

# Sliders are integer 0-100 internally; UI weight is value/100.
SLIDER_MIN = 0
SLIDER_MAX = 100


# ----- Math helpers ------------------------------------------------------


def load_grayscale(
    path: Path,
    target_wh: tuple[int, int],
    *,
    trim_presentation_frame: bool = False,
) -> np.ndarray:
    """Load grayscale data, optionally removing the export's white frame."""
    img = Image.open(path).convert("L")
    if trim_presentation_frame:
        values = np.asarray(img, dtype=np.uint8)
        # The committed simulator exports came through a presentation and
        # carry a thin near-white frame.  Its pixels otherwise become bright
        # edges after downsampling and bias correlation/Auto-fit.  Black RHEED
        # background supplies a stable content bounding box for these assets.
        content = values < 200
        ys, xs = np.nonzero(content)
        if not len(xs):
            raise ValueError(f"Canonical simulator asset has no dark field: {path}")
        top, bottom = int(ys.min()), int(ys.max()) + 1
        left, right = int(xs.min()), int(xs.max()) + 1
        if bottom - top < values.shape[0] // 2 or right - left < values.shape[1] // 2:
            raise ValueError(f"Canonical simulator crop is implausibly small: {path}")
        img = Image.fromarray(values[top:bottom, left:right])
    img = img.resize(target_wh, Image.Resampling.LANCZOS)
    return np.asarray(img, dtype=np.float32)


def load_basis_bundle() -> BasisBundle:
    """Load the four canonical simulator assets plus inactive HTR metadata."""
    assets: list[BasisAsset] = []
    for label in ACTIVE_CLASS_LABELS:
        path = SIMULATED_BASIS_DIR / SIMULATED_BASIS_FILES[label]
        if not path.is_file():
            raise FileNotFoundError(f"Missing canonical simulator basis: {path}")
        assets.append(
            BasisAsset(
                label=label,
                image=load_grayscale(
                    path,
                    CANONICAL_PROCESS_WH,
                    trim_presentation_frame=True,
                ),
                active=True,
                source_kind="simulator",
                source_path=path.relative_to(_REPO_ROOT).as_posix(),
            )
        )
    assets.append(
        BasisAsset(
            label="HTR",
            image=None,
            active=False,
            source_kind="unavailable",
            unavailable_reason="canonical simulator basis pending",
        )
    )
    return BasisBundle(tuple(assets))


def load_class_means(target_wh: tuple[int, int]) -> dict[str, np.ndarray]:
    """Compatibility view of the four active canonical simulator assets.

    HTR is deliberately absent until it has a canonical simulator-frame
    basis.  Returning an empirical HTR image here would mix coordinate frames.
    """
    if tuple(target_wh) != CANONICAL_PROCESS_WH:
        raise ValueError(
            f"Equalizer alignment uses fixed canonical size {CANONICAL_PROCESS_WH}, "
            f"not {target_wh}"
        )
    return load_basis_bundle().active_images(copy=True)


def reconstruct(
    means: dict[str, np.ndarray],
    weights: dict[str, float],
) -> np.ndarray:
    """Weighted sum of class means. Returns float32 clipped to [0, 255]."""
    out = np.zeros_like(next(iter(means.values())))
    for label, w in weights.items():
        if label in means:
            out += w * means[label]
    return np.clip(out, 0, 255)


def auto_fit_details(
    means: dict[str, np.ndarray], target: np.ndarray,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return non-negative raw least-squares coefficients and normalization."""
    labels_in_means = [label for label in ACTIVE_CLASS_LABELS if label in means]
    if not labels_in_means:
        raise ValueError("No active canonical basis images are available")
    basis = np.stack(
        [means[label].flatten() for label in labels_in_means], axis=1,
    )
    t = target.flatten()
    coefficients, *_ = np.linalg.lstsq(basis, t, rcond=None)
    coefficients = np.clip(coefficients, 0, None)
    total = coefficients.sum()
    if total <= 0:
        normalized = np.full(
            len(labels_in_means), 1.0 / len(labels_in_means),
        )
    else:
        normalized = coefficients / total
    return (
        dict(zip(labels_in_means, coefficients)),
        dict(zip(labels_in_means, normalized)),
    )


def auto_fit(
    means: dict[str, np.ndarray], target: np.ndarray,
) -> dict[str, float]:
    """Least-squares fit onto the class-mean basis.

    Clips negative weights to 0 (negative mixture has no physical meaning)
    and normalizes so the weights sum to 1. Falls back to uniform if the
    fit collapses to all-zeros (e.g., black target image).
    """
    _raw, normalized = auto_fit_details(means, target)
    return normalized


def apply_green_palette(arr_u8: np.ndarray) -> np.ndarray:
    """Map grayscale [0, 255] → RGB with a phosphor-green BGW-style ramp.

    Mirrors kSA's BGW (blue → green → white) display convention so the
    rendered images look familiar to growers used to the live RHEED view.
    Pure analysis stays grayscale; this transform is display-only.
    """
    h, w = arr_u8.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    # Green channel scales linearly with intensity (the dominant signal).
    rgb[..., 1] = arr_u8
    # Faint blue underglow at low intensities (BGW ramp's "B" stage).
    rgb[..., 2] = np.clip(arr_u8.astype(np.int16) // 3, 0, 255).astype(np.uint8)
    # White highlights at very high intensities (the "W" tail).
    high_mask = arr_u8 > 200
    if high_mask.any():
        boost = (arr_u8[high_mask].astype(np.int16) - 200) * 3
        boost = np.clip(boost, 0, 255).astype(np.uint8)
        rgb[..., 0][high_mask] = boost
        rgb[..., 2][high_mask] = np.clip(
            rgb[..., 2][high_mask].astype(np.int16) + boost, 0, 255,
        ).astype(np.uint8)
    return rgb


def array_to_pixmap(
    arr: np.ndarray, display_wh: tuple[int, int],
) -> QPixmap:
    """Convert (H, W) float32 in [0, 255] to upscaled phosphor-green QPixmap."""
    arr_u8 = np.clip(arr, 0, 255).astype(np.uint8)
    rgb = np.ascontiguousarray(apply_green_palette(arr_u8))
    h, w, _ = rgb.shape
    qimg = QImage(
        rgb.tobytes(), w, h, w * 3, QImage.Format.Format_RGB888,
    )
    pixmap = QPixmap.fromImage(qimg)
    return pixmap.scaled(
        display_wh[0], display_wh[1],
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


# ----- UI ----------------------------------------------------------------


class EqualizerWindow(QMainWindow):
    """Legacy preview-only window retained for development inspection.

    It must not produce labels. Production Live and Events labels require the
    shared Growth Monitor component, an app-owned accepted calibration, and
    typed capture/QC provenance.
    """

    def __init__(
        self,
        pre_loaded_image: Optional[Path] = None,
        on_save_callback: Optional["Callable[[dict[str, float]], None]"] = None,
    ) -> None:
        super().__init__()
        title = "RHEED Equalizer (preview only)"
        if pre_loaded_image is not None:
            title = f"RHEED Equalizer — {pre_loaded_image.name}"
        self.setWindowTitle(title)
        # Narrower window now that sliders live at the bottom rather than
        # the right column (PI feedback May 19 2026: window was too wide).
        self.resize(1180, 820)

        self.means = load_class_means(PROCESS_WH)
        if not self.means:
            QMessageBox.critical(
                self, "Basis images missing",
                f"Couldn't load the canonical simulator basis from\n"
                f"  {SIMULATED_BASIS_DIR}\n"
                "All four active simulator assets are required.",
            )
            sys.exit(1)
        missing = set(ACTIVE_CLASS_LABELS) - set(self.means)
        if missing:
            QMessageBox.warning(
                self, "Some classes missing",
                f"Missing active canonical basis images: {missing}.",
            )

        self.target: np.ndarray | None = None
        self.target_path: Path | None = None
        self._on_save_callback = on_save_callback

        self._build_ui()
        self._reset_to_uniform()

        # Pre-load the target image if provided (Events tab launcher).
        if pre_loaded_image is not None and pre_loaded_image.exists():
            try:
                self.target = load_grayscale(pre_loaded_image, PROCESS_WH)
                self.target_path = pre_loaded_image
                self.target_label.setPixmap(
                    array_to_pixmap(self.target, DISPLAY_WH),
                )
                self._update_reconstruction()
                # Auto-fit immediately so the grower lands on a sensible
                # starting mixture rather than uniform.
                self.action_autofit()
                self.statusbar.showMessage(
                    f"Loaded {pre_loaded_image.name} (auto-fit)", 4000,
                )
            except Exception as e:
                QMessageBox.warning(
                    self, "Pre-load failed",
                    f"Couldn't pre-load {pre_loaded_image}:\n{e}",
                )

    # ----- Layout -----

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Top bar — buttons
        top = QHBoxLayout()
        for text, slot in [
            ("Open image…",    self.action_open),
            ("Auto-fit",       self.action_autofit),
            ("Normalize",      self.action_normalize),
            ("Reset (uniform)", self.action_reset),
            ("Save label",     self.action_save),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            if text == "Save label":
                self._standalone_save_btn = btn
                btn.setEnabled(False)
                btn.setToolTip(STANDALONE_DISABLED_MESSAGE)
            top.addWidget(btn)
        top.addStretch()
        root.addLayout(top)

        # Middle — target image and your-blend side by side (no sliders here)
        middle = QHBoxLayout()
        middle.setSpacing(8)

        target_group = QGroupBox("Target")
        target_layout = QVBoxLayout(target_group)
        self.target_label = QLabel("No image loaded")
        self.target_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.target_label.setMinimumSize(QSize(*DISPLAY_WH))
        self.target_label.setStyleSheet("background-color: #111; color: #888;")
        target_layout.addWidget(self.target_label)
        middle.addWidget(target_group, 1)

        recon_group = QGroupBox("Your blend")
        recon_layout = QVBoxLayout(recon_group)
        self.recon_label = QLabel()
        self.recon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recon_label.setMinimumSize(QSize(*DISPLAY_WH))
        self.recon_label.setStyleSheet("background-color: #111;")
        recon_layout.addWidget(self.recon_label)
        middle.addWidget(recon_group, 1)

        root.addLayout(middle, 1)

        # Bottom — sliders panel at full window width. Each slider gets a
        # full row with name on the left, wide track in the middle, value
        # on the right. Per PI direction May 8 + May 19 feedback: prominent
        # sliders growers can drag with precision rather than a compact
        # right-column knob row.
        sliders_group = QGroupBox("Reconstruction mixture")
        sliders_layout = QGridLayout(sliders_group)
        sliders_layout.setContentsMargins(12, 16, 12, 12)
        sliders_layout.setHorizontalSpacing(12)
        sliders_layout.setVerticalSpacing(8)
        sliders_layout.setColumnStretch(1, 1)  # slider track expands
        self.sliders: dict[str, QSlider] = {}
        self.value_labels: dict[str, QLabel] = {}
        for i, label in enumerate(CLASS_LABELS):
            name_label = QLabel(f"<b>{label}</b>")
            name_label.setMinimumWidth(90)
            name_label.setTextFormat(Qt.TextFormat.RichText)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(SLIDER_MIN, SLIDER_MAX)
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.setTickInterval(10)
            slider.valueChanged.connect(self._on_slider_changed)
            value_label = QLabel("0.00")
            value_label.setMinimumWidth(60)
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            value_label.setStyleSheet("font-family: monospace;")
            if label in INACTIVE_CLASS_LABELS:
                slider.setValue(0)
                slider.setEnabled(False)
                slider.setToolTip("N/A - canonical basis pending")
                value_label.setText("N/A")
                value_label.setToolTip("Canonical simulator-frame basis pending")
            sliders_layout.addWidget(name_label, i, 0)
            sliders_layout.addWidget(slider, i, 1)
            sliders_layout.addWidget(value_label, i, 2)
            self.sliders[label] = slider
            self.value_labels[label] = value_label
        root.addWidget(sliders_group, 0)

        # Status bar
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.status_sum = QLabel()
        self.status_err = QLabel()
        self.statusbar.addPermanentWidget(self.status_sum)
        self.statusbar.addPermanentWidget(self.status_err)

    # ----- Button actions -----

    def action_open(self) -> None:
        exts = " ".join(f"*{e}" for e in IMAGE_EXTS)
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open RHEED image",
            str(Path.home() / "Downloads"),
            f"Images ({exts})",
        )
        if not path_str:
            return
        try:
            self.target = load_grayscale(Path(path_str), PROCESS_WH)
            self.target_path = Path(path_str)
            self.target_label.setPixmap(
                array_to_pixmap(self.target, DISPLAY_WH),
            )
            self.statusbar.showMessage(
                f"Loaded {self.target_path.name}", 4000,
            )
            self._update_reconstruction()
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))

    def action_autofit(self) -> None:
        if self.target is None:
            self.statusbar.showMessage("Load a target image first", 3000)
            return
        weights = auto_fit(self.means, self.target)
        self._set_weights(weights)

    def action_normalize(self) -> None:
        weights = self._current_weights()
        s = sum(weights.values())
        if s <= 0:
            return
        normalized = {k: v / s for k, v in weights.items()}
        self._set_weights(normalized)

    def action_reset(self) -> None:
        self._reset_to_uniform()

    def action_save(self) -> None:
        """Refuse the legacy path even if invoked programmatically."""
        self.statusbar.showMessage(STANDALONE_DISABLED_MESSAGE, 10000)

    # ----- Slider state plumbing -----

    def _reset_to_uniform(self) -> None:
        w = 1.0 / len(ACTIVE_CLASS_LABELS)
        self._set_weights({label: w for label in ACTIVE_CLASS_LABELS})

    def _set_weights(self, weights: dict[str, float]) -> None:
        """Set sliders without re-firing valueChanged per slider; one update at end."""
        for label, w in weights.items():
            if label in self.sliders:
                slider = self.sliders[label]
                slider.blockSignals(True)
                slider.setValue(int(round(w * SLIDER_MAX)))
                slider.blockSignals(False)
        self._on_slider_changed()

    def _current_weights(self) -> dict[str, float]:
        return {
            label: slider.value() / SLIDER_MAX
            for label, slider in self.sliders.items()
            if label in ACTIVE_CLASS_LABELS
        }

    def _on_slider_changed(self, *_: object) -> None:
        weights = self._current_weights()
        for label, w in weights.items():
            self.value_labels[label].setText(f"{w:.2f}")
        for label in INACTIVE_CLASS_LABELS:
            self.value_labels[label].setText("N/A")
        self._update_reconstruction()

    def _update_reconstruction(self) -> None:
        weights = self._current_weights()
        recon = reconstruct(self.means, weights)
        self.recon_label.setPixmap(array_to_pixmap(recon, DISPLAY_WH))
        total = sum(weights.values())
        normalized = "normalized" if abs(total - 1.0) < 0.01 else "un-normalized"
        self.status_sum.setText(f"Sum: {total:.2f} ({normalized})  ")
        if self.target is not None:
            err = float(np.mean(np.abs(recon - self.target)))
            self.status_err.setText(f"Err: {err:.2f}")
        else:
            self.status_err.setText("")


def main() -> int:
    """Reject direct execution; helpers remain importable by Growth Monitor."""
    print(STANDALONE_DISABLED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
