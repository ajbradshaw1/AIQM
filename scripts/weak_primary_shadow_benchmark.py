"""Stress the bundled weak-primary shadow model in an isolated process."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gui.growth_app import _resolve_weak_primary_ai_repo_root
from gui.weak_primary_shadow import (
    WeakPrimaryShadowBridge,
    _available_system_memory_mb,
)


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _process_memory_mb() -> tuple[float, float]:
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    handle = get_current_process()
    ok = get_process_memory_info(
        handle, ctypes.byref(counters), counters.cb
    )
    if not ok:
        raise ctypes.WinError()
    scale = 1024 * 1024
    return counters.WorkingSetSize / scale, counters.PeakWorkingSetSize / scale


def _percentile(values: list[float], percentile: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load and repeatedly infer with the bundled shadow ensemble."
    )
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=60)
    parser.add_argument("--max-load-seconds", type=float, default=120.0)
    parser.add_argument("--max-p95-ms", type=float, default=1000.0)
    parser.add_argument("--max-peak-working-set-mb", type=float, default=2500.0)
    parser.add_argument("--max-rss-growth-mb", type=float, default=128.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.warmup < 0 or args.iterations < 5:
        parser.error("warmup must be >= 0 and iterations must be >= 5")

    import torch

    available_before_mb = _available_system_memory_mb()
    load_started = time.perf_counter()
    bridge = WeakPrimaryShadowBridge(
        ai_repo_root=_resolve_weak_primary_ai_repo_root(),
        device=args.device,
    )
    load_seconds = time.perf_counter() - load_started
    if args.device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    y, x = np.indices((96, 128), dtype=np.uint16)
    gray = ((x * 7 + y * 13) % 256).astype(np.uint8)
    frame = np.repeat(gray[:, :, None], 3, axis=2)
    for _ in range(args.warmup):
        bridge.classify(frame)

    latencies_ms: list[float] = []
    rss_mb: list[float] = []
    for _ in range(args.iterations):
        started = time.perf_counter_ns()
        result = bridge.classify(frame)
        latencies_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        rss_mb.append(_process_memory_mb()[0])

    _, peak_working_set_mb = _process_memory_mb()
    window = max(1, args.iterations // 5)
    rss_growth_mb = max(
        0.0,
        statistics.median(rss_mb[-window:])
        - statistics.median(rss_mb[:window]),
    )
    checks = {
        "load_time": load_seconds <= args.max_load_seconds,
        "p95_latency": _percentile(latencies_ms, 95) <= args.max_p95_ms,
        "peak_working_set": peak_working_set_mb <= args.max_peak_working_set_mb,
        "rss_growth": rss_growth_mb <= args.max_rss_growth_mb,
        "probability_sum": abs(
            sum(result["conditional_probabilities"].values()) - 1.0
        ) <= 1e-5,
        "shadow_only": result["actionable"] is False,
    }
    report = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "machine": {
            "platform": platform.platform(),
            "python": sys.version,
            "cpu_count": os.cpu_count(),
            "torch": torch.__version__,
            "device": str(bridge.device),
        },
        "model": {
            "ensemble_id": bridge.ensemble_id,
            "checkpoint_count": bridge.checkpoint_count,
            "bundle_family": bridge.bundle_family,
            "brightness_policy": bridge.brightness_policy,
            "output_classes": result["output_classes"],
            "has_1x1_output": "1x1" in result["output_classes"],
            "execution_scope": result["execution_scope"],
            "actionable": result["actionable"],
        },
        "measurements": {
            "available_memory_before_mb": available_before_mb,
            "available_memory_after_mb": _available_system_memory_mb(),
            "load_seconds": load_seconds,
            "latency_ms": {
                "mean": statistics.mean(latencies_ms),
                "p50": _percentile(latencies_ms, 50),
                "p95": _percentile(latencies_ms, 95),
                "p99": _percentile(latencies_ms, 99),
                "max": max(latencies_ms),
            },
            "working_set_mb": {
                "first": rss_mb[0],
                "last": rss_mb[-1],
                "peak": peak_working_set_mb,
                "steady_growth": rss_growth_mb,
            },
            "cuda_peak_allocated_mb": (
                torch.cuda.max_memory_allocated() / (1024 * 1024)
                if args.device == "cuda" else None
            ),
        },
        "thresholds": {
            "max_load_seconds": args.max_load_seconds,
            "max_p95_ms": args.max_p95_ms,
            "max_peak_working_set_mb": args.max_peak_working_set_mb,
            "max_rss_growth_mb": args.max_rss_growth_mb,
        },
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
