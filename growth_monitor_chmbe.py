"""Launch the Growth Monitor configured for the Chalcogenide MBE (Ch-MBE).

This entry point deliberately overrides an inherited chamber selection.  A
workstation-level ``AIQM_CHAMBER=ombe`` must never turn the Ch-MBE launcher
into an O-MBE process.  On Windows it also owns a named mutex for the whole Qt
process lifetime so two monitor instances cannot compete for the same serial,
ADS, and capture interfaces.
"""

from __future__ import annotations

import os
import sys


_MUTEX_NAME = r"Local\AI4MBE.ChMBE.GrowthMonitor"
_ERROR_ALREADY_EXISTS = 183


def _configure_chamber() -> None:
    """Force the chamber before importing any module that caches config."""
    os.environ["AIQM_CHAMBER"] = "chmbe"


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
        raise OSError(ctypes.get_last_error(), "Unable to create Ch-MBE mutex")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise RuntimeError(
            "Ch-MBE Growth Monitor is already running. Close the existing "
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
        # Import only after the chamber has been forced. drivers.config may be
        # cached transitively by the Qt application imports.
        from growth_monitor_app import main as run_growth_monitor

        run_growth_monitor()
        return 0
    finally:
        _release_windows_mutex(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
