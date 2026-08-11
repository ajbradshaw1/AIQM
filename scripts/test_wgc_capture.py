#!/usr/bin/env python3
"""Read-only Bulbasaur probe for detached kSA Live Video WGC capture.

The probe stores sampled raw/cropped images, atomic frame provenance, hashes,
environment identity, and process stability metrics. Large output belongs
under ``logs/`` on the workstation and must not be committed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from drivers.rheed_camera import ScreenGrabCamera  # noqa: E402


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _process_rss_bytes() -> int | None:
    """Return this process's Windows working set without extra packages."""
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        handle, ctypes.byref(counters), counters.cb,
    )
    return int(counters.WorkingSetSize) if ok else None


def _native_thread_count() -> int | None:
    """Count all OS threads owned by this process, including native WGC."""
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ThreadID", ctypes.wintypes.DWORD),
            ("th32OwnerProcessID", ctypes.wintypes.DWORD),
            ("tpBasePri", ctypes.wintypes.LONG),
            ("tpDeltaPri", ctypes.wintypes.LONG),
            ("dwFlags", ctypes.wintypes.DWORD),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32First.restype = ctypes.wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    invalid = ctypes.c_void_p(-1).value
    if snapshot in (0, invalid):
        return None
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    count = 0
    try:
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == os.getpid():
                count += 1
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return count


def _window_diagnostics(hwnd: int) -> dict[str, Any]:
    result: dict[str, Any] = {"source_hwnd": int(hwnd)}
    if sys.platform != "win32":
        return result
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.RECT),
    ]
    user32.GetWindowRect.restype = ctypes.wintypes.BOOL
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    result.update({
        "window_title": title.value,
        "window_rect": [rect.left, rect.top, rect.right, rect.bottom],
        "window_size": [rect.right - rect.left, rect.bottom - rect.top],
    })
    get_dpi = getattr(user32, "GetDpiForWindow", None)
    if get_dpi is not None:
        get_dpi.argtypes = [ctypes.wintypes.HWND]
        get_dpi.restype = ctypes.wintypes.UINT
        result["window_dpi"] = int(get_dpi(hwnd))
        result["window_scale_percent"] = round(
            100.0 * result["window_dpi"] / 96.0, 1,
        )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return float(ordered[index])


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _save_image(
    output_dir: Path,
    name: str,
    image: np.ndarray,
    hashes: list[dict[str, Any]],
) -> Path:
    path = output_dir / name
    Image.fromarray(image).save(path)
    hashes.append({
        "path": path.name,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    })
    return path


def _compare_with_mss(
    camera: ScreenGrabCamera,
    raw_wgc: np.ndarray,
    output_dir: Path,
    hashes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture one explicitly non-synchronized, unobstructed MSS comparison."""
    legacy = ScreenGrabCamera.legacy_mss(
        window_title=camera._window_title,
        crop_chrome=camera._crop_chrome,
        chrome_top_px=camera._chrome_top_px,
        chrome_bottom_px=camera._chrome_bottom_px,
    )
    legacy.connect()
    try:
        raw_mss = legacy._grab_win32()
    finally:
        legacy.disconnect()
    crop_wgc = camera._crop_chrome_pixels(raw_wgc)
    crop_mss = camera._crop_chrome_pixels(raw_mss)
    _save_image(output_dir, "comparison_wgc.png", crop_wgc, hashes)
    _save_image(output_dir, "comparison_mss.png", crop_mss, hashes)
    result: dict[str, Any] = {
        "synchronized": False,
        "instruction": "Run only while Live Video is unobstructed.",
        "wgc_shape": list(crop_wgc.shape),
        "mss_shape": list(crop_mss.shape),
    }
    if crop_wgc.shape == crop_mss.shape:
        delta = np.abs(
            crop_wgc.astype(np.int16) - crop_mss.astype(np.int16),
        )
        result.update({
            "mean_absolute_pixel_difference": float(delta.mean()),
            "max_absolute_pixel_difference": int(delta.max()),
        })
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    default_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("logs") / "validation" / f"wgc_probe_{default_tag}",
    )
    parser.add_argument(
        "--samples", type=int, default=10,
        help="Maximum reads; ignored when --duration-s is positive.",
    )
    parser.add_argument(
        "--duration-s", type=float, default=0.0,
        help="Run for this many seconds (use 3600 for acceptance).",
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument(
        "--save-every", type=int, default=10,
        help="Save image triplets every N reads; use 1 for signal analysis.",
    )
    parser.add_argument("--first-frame-timeout", type=float, default=5.0)
    parser.add_argument("--stale-timeout", type=float, default=5.0)
    parser.add_argument(
        "--compare-mss", action="store_true",
        help="Save one explicit, unobstructed legacy-MSS comparison.",
    )
    args = parser.parse_args()
    if args.samples < 1 or args.interval < 0 or args.save_every < 0:
        parser.error("samples must be >=1; interval/save-every must be >=0")
    args.output_dir.mkdir(parents=True, exist_ok=False)

    camera = ScreenGrabCamera(
        first_frame_timeout_s=args.first_frame_timeout,
        stale_timeout_s=args.stale_timeout,
    )
    started_utc = _utc_now()
    started_local = datetime.now().astimezone().isoformat()
    started_monotonic = time.monotonic()
    start_python_threads = len(threading.enumerate())
    start_native_threads = _native_thread_count()
    start_rss = _process_rss_bytes()
    records: list[dict[str, Any]] = []
    hashes: list[dict[str, Any]] = []
    comparison: dict[str, Any] | None = None
    window: dict[str, Any] = {}
    failure = ""
    exit_code = 0
    last_raw: np.ndarray | None = None

    try:
        camera.connect()
        session = camera._capture_session
        if session is None:
            raise RuntimeError("WGC session was not created")
        window = _window_diagnostics(session.hwnd)
        index = 0
        while True:
            if args.duration_s > 0:
                if time.monotonic() - started_monotonic >= args.duration_s:
                    break
            elif index >= args.samples:
                break

            raw = session.read_latest()
            last_raw = raw.image
            cropped = camera._crop_chrome_pixels(raw.image)
            should_save = index == 0 or (
                args.save_every > 0 and index % args.save_every == 0
            )
            if should_save:
                annotated = camera.visualize_crop(raw.image)
                stem = f"sample_{index:05d}"
                for suffix, image in (
                    ("raw", raw.image),
                    ("cropped", cropped),
                    ("crop_qa", annotated),
                ):
                    _save_image(
                        args.output_dir, f"{stem}_{suffix}.png", image, hashes,
                    )
            previous_ns = (
                records[-1]["captured_monotonic_ns"] if records else None
            )
            records.append({
                "read_index": index,
                "capture_backend": raw.backend,
                "captured_at_utc": raw.captured_at_utc,
                "captured_monotonic_ns": raw.captured_monotonic_ns,
                "capture_gap_ms": (
                    (raw.captured_monotonic_ns - previous_ns) / 1_000_000
                    if previous_ns is not None else None
                ),
                "capture_sequence": raw.sequence,
                "frame_age_ms": raw.age_ms(),
                "source_hwnd": raw.source_hwnd,
                "raw_size": [raw.width, raw.height],
                "cropped_size": [cropped.shape[1], cropped.shape[0]],
                "process_rss_bytes": _process_rss_bytes(),
                "python_thread_count": len(threading.enumerate()),
                "native_thread_count": _native_thread_count(),
            })
            print(
                f"{index + 1}: seq={raw.sequence} "
                f"age={records[-1]['frame_age_ms']:.1f}ms "
                f"native_threads={records[-1]['native_thread_count']}",
                flush=True,
            )
            index += 1
            time.sleep(args.interval)

        if args.compare_mss:
            if last_raw is None:
                raise RuntimeError("No WGC frame is available for MSS comparison")
            comparison = _compare_with_mss(
                camera, last_raw, args.output_dir, hashes,
            )
    except KeyboardInterrupt:
        failure = "Interrupted by operator"
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - probe must preserve evidence
        failure = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        print(f"ERROR: {failure}", file=sys.stderr, flush=True)
    finally:
        try:
            camera.disconnect()
        except Exception as exc:  # noqa: BLE001
            failure = failure or f"Disconnect failed: {type(exc).__name__}: {exc}"
            exit_code = exit_code or 1

    ages = [float(row["frame_age_ms"]) for row in records]
    gaps = [
        float(row["capture_gap_ms"])
        for row in records if row["capture_gap_ms"] is not None
    ]
    sequences = [int(row["capture_sequence"]) for row in records]
    sequence_stalls = sum(
        current <= previous
        for previous, current in zip(sequences, sequences[1:])
    )
    rss_values = [
        int(row["process_rss_bytes"])
        for row in records if row["process_rss_bytes"] is not None
    ]
    end_rss = _process_rss_bytes()
    summary = {
        "status": "failed" if exit_code else "completed",
        "failure": failure,
        "started_at_utc": started_utc,
        "started_at_local": started_local,
        "finished_at_utc": _utc_now(),
        "command": " ".join([sys.executable, *sys.argv]),
        "git_commit": _git_commit(),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "pid": os.getpid(),
        "windows_capture_interpreter": _package_version(
            "windows-capture-interpreter",
        ),
        "configuration": {
            "camera_mode": "screengrab",
            "capture_backend": "wgc",
            "interval_s": args.interval,
            "duration_requested_s": args.duration_s,
            "samples_requested": args.samples,
            "save_every": args.save_every,
            "first_frame_timeout_s": args.first_frame_timeout,
            "stale_timeout_s": args.stale_timeout,
            "crop_chrome": camera._crop_chrome,
            "chrome_top_px": camera._chrome_top_px,
            "chrome_bottom_px": camera._chrome_bottom_px,
        },
        "window": window,
        "raw_artifact_directory": str(args.output_dir.resolve()),
        "reads": len(records),
        "duration_s": time.monotonic() - started_monotonic,
        "sequence_first": sequences[0] if sequences else None,
        "sequence_last": sequences[-1] if sequences else None,
        "non_increasing_sequence_count": sequence_stalls,
        "frame_age_ms": {
            "median": statistics.median(ages) if ages else None,
            "p95": _percentile(ages, 0.95),
            "p99": _percentile(ages, 0.99),
            "max": max(ages) if ages else None,
        },
        "capture_gap_ms": {
            "median": statistics.median(gaps) if gaps else None,
            "p95": _percentile(gaps, 0.95),
            "p99": _percentile(gaps, 0.99),
            "max": max(gaps) if gaps else None,
        },
        "rss_bytes": {
            "start": start_rss,
            "end": end_rss,
            "min_observed": min(rss_values) if rss_values else None,
            "max_observed": max(rss_values) if rss_values else None,
            "growth": (
                end_rss - start_rss
                if end_rss is not None and start_rss is not None else None
            ),
        },
        "thread_count": {
            "python_start": start_python_threads,
            "python_end": len(threading.enumerate()),
            "native_start": start_native_threads,
            "native_end": _native_thread_count(),
        },
        "mss_comparison": comparison,
    }
    metadata_path = args.output_dir / "capture_metadata.json"
    summary_path = args.output_dir / "summary.json"
    _write_json(metadata_path, records)
    _write_json(summary_path, summary)
    for path in (metadata_path, summary_path):
        hashes.append({
            "path": path.name,
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        })
    _write_json(args.output_dir / "sha256_manifest.json", hashes)
    print(json.dumps(summary, indent=2), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
