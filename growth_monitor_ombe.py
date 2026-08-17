"""Launch the Growth Monitor configured for the Oxide MBE (Bulbasaur).

The entry point forces the chamber before importing GUI modules and owns a
process-lifetime mutex on Windows. This prevents a stale machine-level
``AIQM_CHAMBER`` value or a second GUI from silently competing for the same
camera, serial, ADS, and log-reader interfaces.
"""

from __future__ import annotations

import os
import sys


_MUTEX_NAME = r"Local\AI4MBE.OMBE.GrowthMonitor"
_ERROR_ALREADY_EXISTS = 183


def _configure_chamber() -> None:
    """Force O-MBE before any module can cache the active configuration."""
    os.environ["AIQM_CHAMBER"] = "ombe"


def _windows_kernel32():
    """Return kernel32 with pointer-sized mutex APIs fully typed."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _acquire_windows_mutex():
    """Return a process-lifetime Windows mutex handle, or ``None`` elsewhere."""
    if sys.platform != "win32":
        return None

    import ctypes

    kernel32 = _windows_kernel32()
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Unable to create O-MBE mutex")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise RuntimeError(
            "O-MBE Growth Monitor is already running. Close the existing "
            "window before starting another instance."
        )
    return handle


def _release_windows_mutex(handle) -> None:
    if handle is None or sys.platform != "win32":
        return
    _windows_kernel32().CloseHandle(handle)


def main() -> int:
    _configure_chamber()
    try:
        mutex = _acquire_windows_mutex()
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        from growth_monitor_app import main as run_growth_monitor

        run_growth_monitor()
        return 0
    finally:
        _release_windows_mutex(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
