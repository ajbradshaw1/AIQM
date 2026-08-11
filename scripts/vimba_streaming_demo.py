"""Read-only Vimba streaming probe with strict trigger/frame accounting.

The probe uses the asynchronous Vimba callback pattern required for software
triggering.  It records every trigger, callback, and completed image pair in
an append-only JSONL file.  Passing requires exact accounting by default; an
expected camera warm-up loss must be declared with ``--allow-initial-missing``
and remains visible in the summary.

Run only while kSA has released the camera::

    python scripts/vimba_streaming_demo.py --duration 15 --rate 1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from gui.ksa_palette import apply_palette_fixed_range


class JsonlEvidenceWriter:
    """Thread-safe, line-flushed evidence writer used by camera callbacks."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stream = path.open("a", encoding="utf-8", newline="\n")

    def append(self, event: str, **details: Any) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "event": event,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds",
            ),
            "recorded_monotonic_ns": time.perf_counter_ns(),
            **details,
        }
        rendered = json.dumps(record, sort_keys=True, allow_nan=False)
        with self._lock:
            self._stream.write(rendered + "\n")
            self._stream.flush()
        return record

    def close(self) -> None:
        with self._lock:
            if not self._stream.closed:
                self._stream.close()


def evaluate_accounting(
    *,
    triggers_attempted: int,
    triggers_sent: int,
    callbacks_received: int,
    pairs_saved: int,
    trigger_failures: int = 0,
    callback_failures: int = 0,
    save_failures: int = 0,
    allow_initial_missing: int = 0,
) -> dict[str, Any]:
    """Return fail-closed accounting for one streaming run.

    Trigger-to-callback association is order based because Vimba's callback
    does not expose the software-trigger index.  The summary states this
    limitation rather than claiming hardware timestamps or exact exposure
    correlation.
    """
    values = (
        triggers_attempted, triggers_sent, callbacks_received, pairs_saved,
        trigger_failures, callback_failures, save_failures,
        allow_initial_missing,
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("accounting values must be non-negative integers")

    missing_callbacks = max(0, triggers_sent - callbacks_received)
    extra_callbacks = max(0, callbacks_received - triggers_sent)
    unsaved_callbacks = max(0, callbacks_received - pairs_saved)
    extra_saves = max(0, pairs_saved - callbacks_received)
    unexplained_missing = max(0, missing_callbacks - allow_initial_missing)
    passed = all((
        triggers_attempted == triggers_sent + trigger_failures,
        trigger_failures == 0,
        callback_failures == 0,
        save_failures == 0,
        extra_callbacks == 0,
        unsaved_callbacks == 0,
        extra_saves == 0,
        unexplained_missing == 0,
    ))
    return {
        "schema_version": 1,
        "passed": passed,
        "correlation_basis": "callback order after software-trigger order",
        "triggers_attempted": triggers_attempted,
        "triggers_sent": triggers_sent,
        "callbacks_received": callbacks_received,
        "pairs_saved": pairs_saved,
        "trigger_failures": trigger_failures,
        "callback_failures": callback_failures,
        "save_failures": save_failures,
        "missing_callbacks": missing_callbacks,
        "allowed_initial_missing": allow_initial_missing,
        "allowance_scope": (
            "count-only; callback API cannot prove which trigger was missing"
        ),
        "unexplained_missing_callbacks": unexplained_missing,
        "extra_callbacks": extra_callbacks,
        "unsaved_callbacks": unsaved_callbacks,
        "extra_saves": extra_saves,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_pair(
    img: np.ndarray, out_dir: Path, ts: str, idx: int, bit_depth: int,
) -> dict[str, Any]:
    """Save an exact raw plane plus an 8-bit BGW display image."""
    from PIL import Image

    plane = np.asarray(img).squeeze()
    if plane.ndim != 2:
        raise ValueError(f"expected one 2-D camera plane, got {plane.shape}")
    if plane.dtype not in (np.dtype(np.uint8), np.dtype(np.uint16)):
        raise ValueError(f"unsupported camera dtype: {plane.dtype}")

    raw_path = out_dir / f"vimba_streaming_{ts}_{idx:03d}_raw.png"
    # Pillow preserves uint16 PNG samples when handed a uint16 ndarray.
    Image.fromarray(plane).save(raw_path)

    if plane.dtype == np.uint8:
        gray8 = plane
    else:
        max_val = float((1 << bit_depth) - 1)
        scaled = (plane.astype(np.float32) / max_val * 255.0).clip(0, 255)
        gray8 = scaled.astype(np.uint8)
    rgb = apply_palette_fixed_range(gray8, bit_depth=8)
    bgw_path = out_dir / f"vimba_streaming_{ts}_{idx:03d}_bgw.png"
    Image.fromarray(rgb).save(bgw_path)
    return {
        "raw_path": str(raw_path.resolve()),
        "raw_sha256": _sha256(raw_path),
        "bgw_path": str(bgw_path.resolve()),
        "bgw_sha256": _sha256(bgw_path),
        "shape": list(plane.shape),
        "dtype": str(plane.dtype),
        "minimum": int(plane.min()),
        "maximum": int(plane.max()),
        "mean": float(plane.mean()),
        "orientation_transform": "identity (no flip or transpose)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vimba streaming callback probe with strict accounting",
    )
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument(
        "--out", type=Path, default=Path("./vimba_streaming_output"),
    )
    parser.add_argument("--bit-depth", type=int, default=12)
    parser.add_argument(
        "--settle-timeout", type=float, default=2.0,
        help="seconds to wait for callbacks after the final trigger",
    )
    parser.add_argument(
        "--allow-initial-missing", type=int, default=0,
        help="explicitly tolerated warm-up callback loss (default: strict 0)",
    )
    args = parser.parse_args()
    if args.duration <= 0 or args.rate <= 0 or args.settle_timeout < 0:
        parser.error("duration/rate must be positive; settle-timeout non-negative")
    if args.bit_depth not in range(1, 17) or args.allow_initial_missing < 0:
        parser.error("bit-depth must be 1..16; allowance must be non-negative")

    args.out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_target = max(1, int(args.duration * args.rate))
    period = 1.0 / args.rate
    evidence = JsonlEvidenceWriter(
        args.out / f"vimba_streaming_{ts}_events.jsonl",
    )

    try:
        from vmbpy import VmbSystem
    except ImportError:
        evidence.append("fatal_error", error="vmbpy not installed")
        evidence.close()
        print("ERROR: vmbpy not installed. Run: python -m pip install vmbpy")
        return 1

    lock = threading.Lock()
    trigger_records: list[dict[str, Any]] = []
    callback_records: list[dict[str, Any]] = []
    save_records: list[dict[str, Any]] = []
    trigger_errors: list[str] = []
    callback_errors: list[str] = []
    save_errors: list[str] = []
    camera_id = ""
    run_started_at_utc = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds",
    )

    def handler(cam, stream, frame) -> None:
        """Copy before requeueing; save failures remain separate evidence."""
        callback_ns = time.perf_counter_ns()
        with lock:
            idx = len(callback_records)
            callback_records.append({
                "index": idx,
                "callback_monotonic_ns": callback_ns,
            })
        evidence.append(
            "callback_received", callback_index=idx,
            callback_monotonic_ns=callback_ns,
        )
        copy_error = ""
        try:
            img = frame.as_numpy_ndarray().copy().squeeze()
        except Exception as exc:
            copy_error = f"frame copy: {exc}"
            img = None
        queue_error = ""
        try:
            cam.queue_frame(frame)
        except Exception as exc:
            queue_error = f"frame requeue: {exc}"
        if copy_error or queue_error:
            error = "; ".join(value for value in (copy_error, queue_error) if value)
            with lock:
                callback_errors.append(error)
            evidence.append(
                "callback_failed", callback_index=idx, error=error,
            )
            return
        try:
            assert img is not None
            saved = _save_pair(img, args.out, ts, idx, args.bit_depth)
            save_ns = time.perf_counter_ns()
            saved.update({
                "callback_index": idx,
                "callback_monotonic_ns": callback_ns,
                "save_completed_monotonic_ns": save_ns,
                "callback_to_save_ms": (save_ns - callback_ns) / 1_000_000.0,
            })
            with lock:
                save_records.append(saved)
            evidence.append("pair_saved", **saved)
        except Exception as exc:
            with lock:
                save_errors.append(str(exc))
            evidence.append(
                "save_failed", callback_index=idx, error=str(exc),
            )

    print(f"Target: {n_target} triggers over {args.duration}s at {args.rate} Hz")
    print(f"Output: {args.out.resolve()}")
    evidence.append(
        "run_started", target_triggers=n_target, rate_hz=args.rate,
        settle_timeout_s=args.settle_timeout,
        allowed_initial_missing=args.allow_initial_missing,
    )
    session_error = ""
    try:
        with VmbSystem.get_instance() as vmb:
            cams = vmb.get_all_cameras()
            if not cams:
                raise RuntimeError(
                    "no Allied Vision camera; close kSA and verify VimbaX",
                )
            cam = cams[0]
            camera_id = str(cam.get_id())
            evidence.append("camera_opened", camera_id=camera_id)
            with cam:
                cam.TriggerSource.set("Software")
                cam.TriggerSelector.set("FrameStart")
                cam.TriggerMode.set("On")
                cam.AcquisitionMode.set("Continuous")
                cam.start_streaming(handler)
                try:
                    for index in range(n_target):
                        started_ns = time.perf_counter_ns()
                        try:
                            cam.TriggerSoftware.run()
                            sent_ns = time.perf_counter_ns()
                            record = {
                                "index": index,
                                "started_monotonic_ns": started_ns,
                                "sent_monotonic_ns": sent_ns,
                            }
                            with lock:
                                trigger_records.append(record)
                            evidence.append("trigger_sent", **record)
                        except Exception as exc:
                            with lock:
                                trigger_errors.append(str(exc))
                            evidence.append(
                                "trigger_failed", index=index,
                                started_monotonic_ns=started_ns, error=str(exc),
                            )
                        deadline = started_ns + int(period * 1_000_000_000)
                        remaining = (deadline - time.perf_counter_ns()) / 1e9
                        if remaining > 0:
                            time.sleep(remaining)

                    settle_deadline = time.perf_counter() + args.settle_timeout
                    while time.perf_counter() < settle_deadline:
                        with lock:
                            complete = len(callback_records) >= len(trigger_records)
                        if complete:
                            break
                        time.sleep(0.02)
                finally:
                    cam.stop_streaming()
    except Exception as exc:
        session_error = str(exc)
        evidence.append("session_error", error=session_error)

    with lock:
        accounting = evaluate_accounting(
            triggers_attempted=n_target,
            triggers_sent=len(trigger_records),
            callbacks_received=len(callback_records),
            pairs_saved=len(save_records),
            trigger_failures=len(trigger_errors),
            callback_failures=len(callback_errors),
            save_failures=len(save_errors),
            allow_initial_missing=args.allow_initial_missing,
        )
        ordered_latencies = []
        for trigger, callback in zip(trigger_records, callback_records):
            ordered_latencies.append(
                (callback["callback_monotonic_ns"]
                 - trigger["started_monotonic_ns"]) / 1_000_000.0,
            )
    if session_error:
        accounting["passed"] = False
    if any(value < 0 for value in ordered_latencies):
        accounting["passed"] = False
    accounting.update({
        "session_error": session_error,
        "camera_id": camera_id,
        "run_started_at_utc": run_started_at_utc,
        "run_completed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds",
        ),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "requested_duration_s": args.duration,
        "requested_rate_hz": args.rate,
        "bit_depth": args.bit_depth,
        "trigger_to_callback_ms_ordered": ordered_latencies,
        "first_frame": save_records[0] if save_records else None,
        "events_path": str(evidence.path.resolve()),
    })
    evidence.append("run_completed", accounting=accounting)
    evidence.close()
    accounting["events_sha256"] = _sha256(evidence.path)

    summary_json = args.out / f"vimba_streaming_{ts}_summary.json"
    summary_json.write_text(
        json.dumps(accounting, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    summary_text = args.out / f"vimba_streaming_{ts}_summary.txt"
    lines = [
        f"Result: {'PASS' if accounting['passed'] else 'FAIL'}",
        f"Triggers attempted: {accounting['triggers_attempted']}",
        f"Triggers sent: {accounting['triggers_sent']}",
        f"Callbacks received: {accounting['callbacks_received']}",
        f"Image pairs saved: {accounting['pairs_saved']}",
        f"Unexplained missing callbacks: "
        f"{accounting['unexplained_missing_callbacks']}",
        f"Allowed initial missing: {accounting['allowed_initial_missing']}",
        f"Session error: {session_error}",
        f"JSON summary: {summary_json.resolve()}",
    ]
    summary_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return 0 if accounting["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
