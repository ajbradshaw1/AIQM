#!/usr/bin/env python3
"""Camera acquisition provenance reaches session_metadata.json.

Exposure and gain come from the camera's persistent user set, which the
GUI does not set. Recording them is what lets an archived session be
interpreted later; a session that ends without them is ambiguous on the
axis the 2026-08-05 parity audit called the main classifier-input risk.

These cover the wiring rather than the driver: that both session-ending
paths record it, and that a session ending without a camera worker
degrades instead of raising in the middle of the save.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui.growth_app import GrowthApp  # noqa: E402


class _Monitor:
    def get_session_metadata(self) -> dict:
        return {"grower": "AJ", "sample_id": "STO-test"}


class _Worker:
    def __init__(self, settings: dict):
        self.sensor_settings_at_connect = settings


def _app(worker) -> GrowthApp:
    """A GrowthApp shell with only what the helper touches.

    Constructing the real thing needs a QApplication and the full widget
    tree; the helper depends on exactly two attributes.
    """
    app = GrowthApp.__new__(GrowthApp)
    app.monitor = _Monitor()
    app.camera_worker = worker
    return app


def test_settings_recorded_when_present() -> None:
    settings = {"exposure_us": 300000.0, "gain": 0, "black_level": 35.0}
    metadata = _app(_Worker(settings))._session_metadata_with_camera()
    assert metadata["camera_sensor_settings_at_connect"] == settings
    # The base metadata must survive intact.
    assert metadata["grower"] == "AJ"


def test_missing_worker_does_not_raise() -> None:
    """camera_worker is Optional; a session can end without one.

    This runs immediately before save_session_metadata, so raising here
    would cost the metadata write and the growth-log export that follows
    it — a session's records lost to a null check.
    """
    metadata = _app(None)._session_metadata_with_camera()
    assert "camera_sensor_settings_at_connect" not in metadata
    assert metadata["grower"] == "AJ"


def test_sensorless_backend_omits_the_key() -> None:
    """screengrab/dummy report {} — omitted rather than written empty."""
    metadata = _app(_Worker({}))._session_metadata_with_camera()
    assert "camera_sensor_settings_at_connect" not in metadata


def test_both_session_end_paths_use_the_helper() -> None:
    """STOP and window-close must not write different metadata.

    A session ended by closing the window is exactly as much a session as
    one ended with STOP. Divergence here would be a silent gap in the
    archive rather than a visible failure, so pin that both paths go
    through the shared helper.
    """
    source = Path(__file__).parent.parent / "gui" / "growth_app.py"
    text = source.read_text(encoding="utf-8")
    saves = text.count("self.growth_log.save_session_metadata(")
    through_helper = text.count("self._session_metadata_with_camera()")
    assert saves == through_helper, (
        f"{saves} session_metadata.json writes but {through_helper} go "
        "through the helper — one path writes without camera provenance"
    )
    assert saves == 2, f"expected STOP and window-close only, found {saves}"
    # _on_export also calls monitor.get_session_metadata() directly and is
    # correct to: it builds the xlsx growth log from the OMBE template,
    # which has no camera fields, and does not touch session_metadata.json.
    # Keyed on save_session_metadata rather than get_session_metadata so
    # that distinction stays intentional instead of accidental.


TESTS = [
    test_settings_recorded_when_present,
    test_missing_worker_does_not_raise,
    test_sensorless_backend_omits_the_key,
    test_both_session_end_paths_use_the_helper,
]


def main() -> int:
    failures = []
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
