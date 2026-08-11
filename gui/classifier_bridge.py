"""
Bridge between the Classifier2 ML model and the Growth Monitor GUI.

Loads the trained BradleyTerryModel once at startup, pre-computes ideal/bad
reference scores, and exposes a lightweight ``classify(frame)`` method for
real-time inference on RHEED camera frames.

Requires:
    - AI_for_quantum repo on sys.path (or CLASSIFIER2_ROOT configured)
    - ``Classifier2/artifacts/best_model.pth`` (trained model)
    - ``Data/STO_ideal_*/`` directories (reference images)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np

# Label constants live in gui/recon_labels.py so the bridge, the main-tab
# sliders, and any future component share one source of truth. The
# ``GUI_LABELS`` / ``C2_LABELS`` names are kept as aliases so existing
# importers (scripts/classifier_demo.py) keep working; new code should
# import from ``gui.recon_labels`` directly.
from gui.recon_labels import RECON_LABELS, CLASSIFIER2_TO_GUI

GUI_LABELS = RECON_LABELS
C2_LABELS = list(CLASSIFIER2_TO_GUI.keys())
_C2_TO_GUI = CLASSIFIER2_TO_GUI


class ClassifierBridge:
    """Loads Classifier2 and runs inference on raw RHEED frames.

    Parameters
    ----------
    ai_repo_root : str | Path
        Path to the AI_for_quantum repository root.
    model_path : str | Path | None
        Override path to ``best_model.pth``.  Defaults to
        ``<ai_repo_root>/Classifier2/artifacts/best_model.pth``.
    bad_threshold : float
        Confidence threshold for "bad image" detection (default 0.6).
    """

    # The deployed ``evaluate.classify_winrate`` path accepts exactly one
    # image.  Do not advertise a temporal warm-up until a bridge actually
    # supplies the 32-frame tensor expected by the experimental GRU.
    input_mode = "single_frame"
    uses_temporal_history = False
    history_frames_required = 0

    def __init__(
        self,
        ai_repo_root: str | Path,
        model_path: Optional[str | Path] = None,
        bad_threshold: float = 0.6,
    ):
        self._repo = Path(ai_repo_root)
        self._bad_threshold = bad_threshold

        # Auto-detect the Classifier2 layout. Justin's GitHub origin/main is
        # still pre-reorg (Classifier2/ at repo root); some local clones (AJ's
        # Mac) are post-reorg (src/classifiers/classifier2/). Prefer the
        # post-reorg path when present, fall back to the pre-reorg one.
        candidates = [
            self._repo / "src" / "classifiers" / "classifier2",
            self._repo / "Classifier2",
        ]
        c2_dir_path = next((p for p in candidates if p.exists()), candidates[0])
        c2_dir = str(c2_dir_path)
        if c2_dir not in sys.path:
            sys.path.insert(0, c2_dir)

        import torch
        from evaluate import load_model, load_all_ideal_scores, load_bad_image_scores

        self._torch = torch

        # Resolve model path against whichever layout we detected.
        if model_path is None:
            model_path = c2_dir_path / "artifacts" / "best_model.pth"

        # Store resolved path publicly so callers (e.g. ClassifierWorker)
        # can read filename + mtime for the model-version indicator.
        self.model_path = Path(model_path)
        self._model, self._device = load_model(str(model_path))

        # Pre-compute reference scores (one-time cost).
        self._ideal_scores = load_all_ideal_scores(self._model, self._device)
        self._bad_scores = load_bad_image_scores(self._model, self._device)

        # Import the classify function.
        # Note (June 16 2026): ``preprocess_image`` used to be imported here
        # and stored on ``self._preprocess`` but was never used downstream and
        # is no longer exported from ``evaluate.py`` after the upstream
        # reorganization. Removed to unbreak the import on all current clones.
        from evaluate import classify_winrate as _cw
        self._classify_winrate = _cw

    # ------------------------------------------------------------------

    def classify(self, frame: np.ndarray) -> dict:
        """Classify a single RHEED frame (H x W x 3 uint8 numpy array).

        Returns a dict with keys:
            predicted_class  – GUI-friendly label (from GUI_LABELS)
            classification_scores – {gui_label: float} win-rates (0-1)
            is_bad           – bool
            bad_confidence   – float
            quality          – float (0-1)
        """
        # Save frame to a temporary file (classify_winrate expects a path).
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            from PIL import Image
            Image.fromarray(frame).save(tmp.name)
            tmp.close()

            raw = self._classify_winrate(
                self._model,
                tmp.name,
                self._device,
                self._ideal_scores,
                bad_scores=self._bad_scores,
                bad_threshold=self._bad_threshold,
            )
        finally:
            os.unlink(tmp.name)

        # Translate Classifier2 labels → GUI labels.
        c2_scores = raw.get("classification_scores", {})
        gui_scores = {_C2_TO_GUI.get(k, k): v for k, v in c2_scores.items()}

        predicted = raw.get("predicted_class", "")
        gui_predicted = _C2_TO_GUI.get(predicted, predicted)

        return {
            "predicted_class": gui_predicted,
            "classification_scores": gui_scores,
            "is_bad": raw.get("is_bad", False),
            "bad_confidence": raw.get("bad_confidence", 0.0),
            "quality": raw.get("quality"),
        }
