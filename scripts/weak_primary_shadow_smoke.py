"""Load the bundled brightness-robust four-output shadow and infer once."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.growth_app import _resolve_weak_primary_ai_repo_root
from gui.weak_primary_shadow import WeakPrimaryShadowBridge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    bridge = WeakPrimaryShadowBridge(
        ai_repo_root=_resolve_weak_primary_ai_repo_root(),
        device=args.device,
    )
    result = bridge.classify(np.zeros((96, 128, 3), dtype=np.uint8))
    print(json.dumps({
        "status": "PASS",
        "ensemble_id": bridge.ensemble_id,
        "checkpoint_count": bridge.checkpoint_count,
        "bundle_family": bridge.bundle_family,
        "brightness_policy": bridge.brightness_policy,
        "output_classes": result["output_classes"],
        "has_1x1_output": "1x1" in result["output_classes"],
        "device": str(bridge.device),
        "actionable": result["actionable"],
        "probability_sum": sum(result["conditional_probabilities"].values()),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
