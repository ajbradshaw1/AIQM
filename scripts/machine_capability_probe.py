#!/usr/bin/env python3
"""Inventory what a lab PC can actually run, without touching the hardware.

Motivation: before asking whether a local model or agent can help with data
taking, we need the machine's real specification and real headroom. Neither is
recorded anywhere in this repo -- `bulbasaur_env.md` lists Python packages and
instrument wiring, but no CPU, no RAM, no GPU, no disk, and no answer to the
decisive question of whether the lab network will let a model be downloaded at
all.

The probe is read-only and instrument-free. It imports no driver, opens no COM
port, claims no camera, and writes only to its own output directory. It is safe
to run while the Growth Monitor is up -- in fact ``--sample-seconds`` is
designed for exactly that, because idle specification numbers say nothing about
headroom during acquisition.

Sections:

  A  identity + OS
  B  CPU: model, cores, clock, SIMD generation
  C  memory: installed and currently available
  D  GPU: name, VRAM, driver  (decides local-model class)
  E  storage: free space and media type per drive
  F  Python + relevant package inventory
  G  reachability + install rights  (decides whether a download is possible)
  H  microbenchmarks: FLOPS, memory bandwidth, disk write
  I  load sampling: CPU/RAM occupancy over a window

Exit code is always 0 -- this is a description, not a test.

Usage:
    python scripts/machine_capability_probe.py
    python scripts/machine_capability_probe.py --sample-seconds 120
    python scripts/machine_capability_probe.py --skip-network
    python scripts/machine_capability_probe.py --out probe_output/bulbasaur
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROBE_SCHEMA_VERSION = "machine-capability-v1"

# Hosts whose reachability decides what is possible at all. Probed with a bare
# TCP connect -- no request is sent, nothing is downloaded, no credential is
# used. A blocked lab network is the single most common reason a "just run a
# local model" plan dies on arrival.
REACHABILITY_TARGETS = (
    ("pypi.org", 443, "pip install anything"),
    ("files.pythonhosted.org", 443, "pip wheel downloads (separate CDN)"),
    ("github.com", 443, "git pull of this repo"),
    ("huggingface.co", 443, "model weight download"),
    ("cdn-lfs.huggingface.co", 443, "HF large-file CDN (separate host)"),
    ("ollama.com", 443, "ollama model pull"),
    ("api.anthropic.com", 443, "hosted-model fallback"),
)

# Packages that change what class of local workload is feasible. Absence is a
# finding, not an error.
PACKAGES_OF_INTEREST = (
    "numpy", "torch", "torchvision", "onnxruntime", "onnx",
    "transformers", "llama_cpp", "openvino", "cv2", "PIL",
    "psutil", "PyQt6", "pyqtgraph", "vmbpy", "pymodbus",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(cmd: list[str], timeout: float = 20.0) -> Optional[str]:
    """Run a read-only system query, returning None on any failure."""
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    out = completed.stdout.strip()
    return out or None


def _powershell_json(query: str) -> Any:
    """Query Windows CIM and parse the JSON result, or None."""
    raw = _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"{query} | ConvertTo-Json -Compress -Depth 3",
    ])
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _as_list(value: Any) -> list[Any]:
    """CIM returns a bare object for one result and a list for many."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ---------------------------------------------------------------------------
# A. identity
# ---------------------------------------------------------------------------

def section_identity() -> dict[str, Any]:
    return {
        "probed_at_utc": _utc_now_iso(),
        "hostname": socket.gethostname(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cwd": str(Path.cwd()),
    }


# ---------------------------------------------------------------------------
# B. CPU
# ---------------------------------------------------------------------------

def section_cpu() -> dict[str, Any]:
    info: dict[str, Any] = {
        "model": platform.processor() or "",
        "logical_cores": os.cpu_count(),
        "physical_cores": None,
        "max_clock_mhz": None,
        "simd": [],
        "notes": [],
    }

    if platform.system() == "Windows":
        cim = _powershell_json(
            "Get-CimInstance Win32_Processor | "
            "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,"
            "MaxClockSpeed,L3CacheSize"
        )
        entries = _as_list(cim)
        if entries:
            first = entries[0]
            info["model"] = first.get("Name", info["model"]) or info["model"]
            info["physical_cores"] = sum(
                e.get("NumberOfCores") or 0 for e in entries
            ) or None
            info["logical_cores"] = sum(
                e.get("NumberOfLogicalProcessors") or 0 for e in entries
            ) or info["logical_cores"]
            info["max_clock_mhz"] = first.get("MaxClockSpeed")
            info["l3_cache_kb"] = first.get("L3CacheSize")
            info["socket_count"] = len(entries)
    elif platform.system() == "Darwin":
        info["model"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or info["model"]
        cores = _run(["sysctl", "-n", "hw.physicalcpu"])
        info["physical_cores"] = int(cores) if cores and cores.isdigit() else None
        features = _run(["sysctl", "-n", "machdep.cpu.features"]) or ""
        leaf7 = _run(["sysctl", "-n", "machdep.cpu.leaf7_features"]) or ""
        blob = f"{features} {leaf7}".upper()
        info["simd"] = [f for f in ("AVX512F", "AVX2", "AVX1.0", "SSE4.2") if f in blob]
    else:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError:
            cpuinfo = ""
        for line in cpuinfo.splitlines():
            if line.startswith("model name"):
                info["model"] = line.split(":", 1)[1].strip()
                break
        blob = cpuinfo.lower()
        info["simd"] = [
            f for f in ("avx512f", "avx2", "avx", "sse4_2") if f" {f} " in blob
        ]

    # SIMD generation is the practical gate on CPU inference speed: an AVX2
    # machine runs quantized transformer kernels roughly an order of magnitude
    # faster than an SSE-only one, and llama.cpp ships AVX2 builds by default.
    if platform.system() == "Windows" and not info["simd"]:
        info["notes"].append(
            "SIMD flags not queried on Windows -- infer from the CPU model, or "
            "run the numpy GFLOPS benchmark below as a proxy."
        )
    return info


# ---------------------------------------------------------------------------
# C. memory
# ---------------------------------------------------------------------------

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_memory() -> Optional[dict[str, Any]]:
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    return {
        "total_mb": round(status.ullTotalPhys / 1024 / 1024),
        "available_mb": round(status.ullAvailPhys / 1024 / 1024),
        "load_percent": status.dwMemoryLoad,
        "pagefile_total_mb": round(status.ullTotalPageFile / 1024 / 1024),
    }


def section_memory() -> dict[str, Any]:
    if platform.system() == "Windows":
        win = _windows_memory()
        if win:
            return win
    if platform.system() == "Darwin":
        total = _run(["sysctl", "-n", "hw.memsize"])
        return {
            "total_mb": round(int(total) / 1024 / 1024) if total else None,
            "available_mb": None,
            "notes": "available_mb needs vm_stat parsing; not needed on Mac",
        }
    try:
        meminfo = Path("/proc/meminfo").read_text()
    except OSError:
        return {"total_mb": None, "available_mb": None}
    fields = {}
    for line in meminfo.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0]) // 1024
    return {
        "total_mb": fields.get("MemTotal"),
        "available_mb": fields.get("MemAvailable"),
    }


# ---------------------------------------------------------------------------
# D. GPU
# ---------------------------------------------------------------------------

def section_gpu() -> dict[str, Any]:
    result: dict[str, Any] = {"adapters": [], "nvidia_smi": None}

    if platform.system() == "Windows":
        cim = _powershell_json(
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterRAM,DriverVersion,VideoProcessor,"
            "CurrentHorizontalResolution,CurrentVerticalResolution"
        )
        for entry in _as_list(cim):
            ram = entry.get("AdapterRAM")
            result["adapters"].append({
                "name": entry.get("Name"),
                # AdapterRAM is a signed 32-bit field and saturates at 4 GB;
                # treat it as a floor, never as the real VRAM figure.
                "adapter_ram_mb": round(ram / 1024 / 1024) if ram else None,
                "driver_version": entry.get("DriverVersion"),
                "video_processor": entry.get("VideoProcessor"),
                "resolution": (
                    f'{entry.get("CurrentHorizontalResolution")}x'
                    f'{entry.get("CurrentVerticalResolution")}'
                ),
            })

    smi = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version,compute_cap",
        "--format=csv,noheader",
    ])
    result["nvidia_smi"] = smi
    result["cuda_capable"] = bool(smi)
    return result


# ---------------------------------------------------------------------------
# E. storage
# ---------------------------------------------------------------------------

def section_storage() -> dict[str, Any]:
    result: dict[str, Any] = {"volumes": [], "media_types": []}

    candidates: list[str] = []
    if platform.system() == "Windows":
        candidates = [f"{letter}:\\" for letter in "CDEFG"]
        media = _powershell_json(
            "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,Size,BusType"
        )
        for entry in _as_list(media):
            size = entry.get("Size")
            result["media_types"].append({
                "name": entry.get("FriendlyName"),
                # HDD vs SSD decides whether frame writes can keep up with
                # acquisition, and whether a multi-GB model load is seconds
                # or minutes.
                "media_type": entry.get("MediaType"),
                "bus": entry.get("BusType"),
                "size_gb": round(size / 1024**3) if size else None,
            })
    else:
        candidates = ["/", str(Path.home())]

    seen: set[str] = set()
    for path in candidates:
        if not Path(path).exists():
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        key = f"{usage.total}"
        if key in seen:
            continue
        seen.add(key)
        result["volumes"].append({
            "path": path,
            "total_gb": round(usage.total / 1024**3, 1),
            "free_gb": round(usage.free / 1024**3, 1),
        })
    return result


# ---------------------------------------------------------------------------
# F. Python environment
# ---------------------------------------------------------------------------

def section_python() -> dict[str, Any]:
    import importlib
    import importlib.metadata as md

    packages: dict[str, Any] = {}
    for name in PACKAGES_OF_INTEREST:
        dist_name = {"cv2": "opencv-python", "PIL": "Pillow",
                     "llama_cpp": "llama-cpp-python"}.get(name, name)
        try:
            version = md.version(dist_name)
        except md.PackageNotFoundError:
            version = None
        packages[name] = version

    torch_detail: dict[str, Any] = {}
    if packages.get("torch"):
        try:
            torch = importlib.import_module("torch")
            torch_detail = {
                "version": torch.__version__,
                "cuda_available": bool(torch.cuda.is_available()),
                "default_threads": torch.get_num_threads(),
            }
        except Exception as exc:  # noqa: BLE001 -- inventory must not fail
            torch_detail = {"import_error": repr(exc)}

    return {
        "executable": sys.executable,
        "version": sys.version.split()[0],
        "in_virtualenv": sys.prefix != sys.base_prefix,
        "packages": packages,
        "torch": torch_detail,
    }


# ---------------------------------------------------------------------------
# G. reachability + rights
# ---------------------------------------------------------------------------

def section_reachability(skip: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"skipped": skip, "targets": [], "is_admin": None,
                              "proxy_env": {}}

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"):
        if os.environ.get(var):
            result["proxy_env"][var] = os.environ[var]

    if platform.system() == "Windows":
        try:
            result["is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            result["is_admin"] = None
    else:
        result["is_admin"] = os.geteuid() == 0 if hasattr(os, "geteuid") else None

    if skip:
        return result

    for host, port, why in REACHABILITY_TARGETS:
        started = time.perf_counter()
        reachable, detail = False, ""
        try:
            with socket.create_connection((host, port), timeout=5.0):
                reachable = True
        except OSError as exc:
            detail = repr(exc)
        result["targets"].append({
            "host": host,
            "port": port,
            "matters_for": why,
            "reachable": reachable,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": detail,
        })
    return result


# ---------------------------------------------------------------------------
# H. microbenchmarks
# ---------------------------------------------------------------------------

def section_benchmarks(out_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}

    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy unavailable -- no benchmarks run"}

    # Dense sgemm is the closest single number to "how fast would a quantized
    # model decode here": both are BLAS-bound, and it also reveals whether the
    # installed numpy has a real BLAS or the slow reference fallback.
    size = 1024
    a = np.random.rand(size, size).astype(np.float32)
    b = np.random.rand(size, size).astype(np.float32)
    a @ b  # warm the BLAS threadpool
    samples = []
    for _ in range(5):
        started = time.perf_counter()
        a @ b
        samples.append(time.perf_counter() - started)
    best = min(samples)
    result["sgemm_1024"] = {
        "best_seconds": round(best, 5),
        "median_seconds": round(statistics.median(samples), 5),
        "gflops": round((2 * size**3) / best / 1e9, 1),
    }

    # Streaming copy bandwidth -- the other half of inference cost, since
    # weight streaming from RAM dominates decode on machines without a GPU.
    big = np.empty(64 * 1024 * 1024 // 4, dtype=np.float32)
    big.fill(1.0)
    started = time.perf_counter()
    for _ in range(4):
        big.copy()
    elapsed = time.perf_counter() - started
    result["memory_copy"] = {
        "gb_per_s": round((big.nbytes * 4 * 2) / elapsed / 1024**3, 2),
    }

    # Sequential disk write, sized like a RHEED frame burst.
    payload = os.urandom(8 * 1024 * 1024)
    with tempfile.NamedTemporaryFile(dir=out_dir, delete=False) as handle:
        temp_path = Path(handle.name)
        started = time.perf_counter()
        for _ in range(8):
            handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        elapsed = time.perf_counter() - started
    temp_path.unlink(missing_ok=True)
    result["disk_write"] = {
        "mb_per_s": round((len(payload) * 8) / 1024 / 1024 / elapsed, 1),
    }
    return result


# ---------------------------------------------------------------------------
# I. load sampling
# ---------------------------------------------------------------------------

def section_load(seconds: int) -> dict[str, Any]:
    """Sample occupancy over a window -- run this WITH the GUI up."""
    if seconds <= 0:
        return {"skipped": True}

    samples: list[dict[str, Any]] = []
    deadline = time.time() + seconds
    interval = 2.0

    while time.time() < deadline:
        entry: dict[str, Any] = {"at_utc": _utc_now_iso()}
        if platform.system() == "Windows":
            mem = _windows_memory()
            if mem:
                entry["memory_load_percent"] = mem["load_percent"]
                entry["available_mb"] = mem["available_mb"]
            cpu = _run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_Processor | "
                "Measure-Object -Property LoadPercentage -Average).Average",
            ], timeout=10.0)
            if cpu and cpu.replace(".", "").isdigit():
                entry["cpu_load_percent"] = float(cpu)
        else:
            load1, load5, load15 = os.getloadavg()
            entry["loadavg"] = [round(load1, 2), round(load5, 2), round(load15, 2)]
        samples.append(entry)
        time.sleep(interval)

    summary: dict[str, Any] = {"window_seconds": seconds, "samples": samples}
    cpu_values = [s["cpu_load_percent"] for s in samples if "cpu_load_percent" in s]
    if cpu_values:
        summary["cpu_load_percent"] = {
            "mean": round(statistics.mean(cpu_values), 1),
            "max": max(cpu_values),
        }
    avail = [s["available_mb"] for s in samples if "available_mb" in s]
    if avail:
        summary["available_mb"] = {"min": min(avail), "mean": round(statistics.mean(avail))}
    return summary


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def render_markdown(report: dict[str, Any]) -> str:
    ident = report["identity"]
    cpu = report["cpu"]
    mem = report["memory"]
    gpu = report["gpu"]
    lines = [
        f"# Machine capability probe — {ident['hostname']}",
        "",
        f"- Probed: {ident['probed_at_utc']}",
        f"- Platform: {ident['platform']}",
        f"- User: {ident['user']}  (admin: {report['reachability'].get('is_admin')})",
        "",
        "## Compute",
        "",
        f"- CPU: {cpu.get('model')}",
        f"- Cores: {cpu.get('physical_cores')} physical / "
        f"{cpu.get('logical_cores')} logical @ {cpu.get('max_clock_mhz')} MHz",
        f"- SIMD: {', '.join(cpu.get('simd') or []) or 'not queried'}",
        f"- RAM: {mem.get('total_mb')} MB total, {mem.get('available_mb')} MB free",
        f"- CUDA: {gpu.get('cuda_capable')}  ({gpu.get('nvidia_smi') or 'no nvidia-smi'})",
    ]
    for adapter in gpu.get("adapters", []):
        lines.append(f"  - GPU: {adapter['name']} (driver {adapter['driver_version']})")

    lines += ["", "## Storage", ""]
    for vol in report["storage"]["volumes"]:
        lines.append(f"- {vol['path']}: {vol['free_gb']} GB free of {vol['total_gb']} GB")
    for disk in report["storage"]["media_types"]:
        lines.append(f"- {disk['name']}: {disk['media_type']} / {disk['bus']} / {disk['size_gb']} GB")

    lines += ["", "## Reachability (decides whether a model can be fetched)", ""]
    if report["reachability"]["skipped"]:
        lines.append("- skipped")
    else:
        for target in report["reachability"]["targets"]:
            mark = "OK  " if target["reachable"] else "BLOCKED"
            lines.append(
                f"- {mark} {target['host']}:{target['port']} — {target['matters_for']}"
            )
    if report["reachability"]["proxy_env"]:
        lines.append(f"- proxy env: {report['reachability']['proxy_env']}")

    lines += ["", "## Benchmarks", ""]
    bench = report["benchmarks"]
    if "error" in bench:
        lines.append(f"- {bench['error']}")
    else:
        lines.append(
            f"- sgemm 1024³: {bench['sgemm_1024']['gflops']} GFLOPS "
            f"({bench['sgemm_1024']['best_seconds']} s best of 5)"
        )
        lines.append(f"- memory copy: {bench['memory_copy']['gb_per_s']} GB/s")
        lines.append(f"- disk write: {bench['disk_write']['mb_per_s']} MB/s")

    lines += ["", "## Python", ""]
    py = report["python"]
    lines.append(f"- {py['version']} at `{py['executable']}` (venv: {py['in_virtualenv']})")
    present = [f"{k} {v}" for k, v in py["packages"].items() if v]
    absent = [k for k, v in py["packages"].items() if not v]
    lines.append(f"- installed: {', '.join(present) or 'none of interest'}")
    lines.append(f"- absent: {', '.join(absent) or 'none'}")
    if py["torch"]:
        lines.append(f"- torch detail: {py['torch']}")

    load = report["load"]
    if not load.get("skipped"):
        lines += ["", f"## Load over {load['window_seconds']} s", ""]
        if "cpu_load_percent" in load:
            lines.append(
                f"- CPU: mean {load['cpu_load_percent']['mean']}%, "
                f"peak {load['cpu_load_percent']['max']}%"
            )
        if "available_mb" in load:
            lines.append(
                f"- RAM free: min {load['available_mb']['min']} MB, "
                f"mean {load['available_mb']['mean']} MB"
            )
        if any("loadavg" in s for s in load.get("samples", [])):
            last = [s for s in load["samples"] if "loadavg" in s][-1]
            lines.append(f"- loadavg (last sample): {last['loadavg']}")

    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: probe_output/machine_<timestamp>)")
    parser.add_argument("--sample-seconds", type=int, default=0,
                        help="sample CPU/RAM occupancy for this long; run with the GUI up")
    parser.add_argument("--skip-network", action="store_true",
                        help="skip the reachability probe (no outbound connections at all)")
    parser.add_argument("--skip-benchmarks", action="store_true",
                        help="skip microbenchmarks (they briefly saturate the CPU)")
    args = parser.parse_args(argv)

    out_dir = args.out or (
        Path(__file__).resolve().parent.parent
        / "probe_output" / time.strftime("machine_%Y%m%d_%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "identity": section_identity(),
        "cpu": section_cpu(),
        "memory": section_memory(),
        "gpu": section_gpu(),
        "storage": section_storage(),
        "python": section_python(),
        "reachability": section_reachability(skip=args.skip_network),
        "benchmarks": ({"error": "skipped by flag"} if args.skip_benchmarks
                       else section_benchmarks(out_dir)),
    }
    report["load"] = section_load(args.sample_seconds)

    json_path = out_dir / "machine_capability.json"
    md_path = out_dir / "machine_capability.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")

    print(markdown)
    print(f"\nWrote {json_path}\nWrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
