"""Windows Graphics Capture adapter for window-scoped RHEED acquisition.

The third-party package owns the native WGC/D3D session.  This module keeps
that dependency behind a small synchronous interface suitable for the
existing one-Hz camera worker.  Native callback buffers are copied
immediately because their lifetime ends when the callback returns.
"""

from __future__ import annotations

import itertools
import logging
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

# Process-wide sequence so reconnecting the same HWND during one Growth Monitor
# session cannot reuse sequence IDs. ``next`` is atomic under CPython's GIL.
_CAPTURE_SEQUENCE = itertools.count(1)


class WindowCaptureError(RuntimeError):
    """Base error for a failed or unavailable window capture session."""


class WindowCaptureTimeout(WindowCaptureError):
    """Raised when the session produces no first frame or becomes stale."""


@dataclass(frozen=True)
class CapturedFrame:
    """One image and the provenance recorded in the same callback."""

    image: np.ndarray
    captured_at_utc: str
    captured_monotonic_ns: int
    sequence: int
    source_hwnd: int
    width: int
    height: int
    backend: str = "wgc"

    def with_image(self, image: np.ndarray) -> "CapturedFrame":
        """Return the same provenance paired with a transformed image."""
        height, width = image.shape[:2]
        return replace(self, image=image, width=width, height=height)

    def age_ms(self, now_ns: Optional[int] = None) -> float:
        current_ns = time.monotonic_ns() if now_ns is None else now_ns
        return max(0.0, (current_ns - self.captured_monotonic_ns) / 1_000_000)


def bgra_to_rgb(frame_buffer: np.ndarray) -> np.ndarray:
    """Copy a WGC BGRA frame into contiguous RGB uint8 storage."""
    array = np.asarray(frame_buffer)
    if array.ndim != 3 or array.shape[2] < 3:
        raise WindowCaptureError(
            f"WGC returned an invalid frame shape: {array.shape!r}"
        )
    return np.ascontiguousarray(array[:, :, :3][:, :, ::-1], dtype=np.uint8)


class WindowsGraphicsCapture:
    """Persistent latest-frame WGC session for one HWND.

    ``capture_factory`` is an injection seam used by the platform-independent
    unit tests.  Production leaves it unset and imports ``WindowsCapture``
    lazily, so dummy/direct GUI modes do not require the optional WGC wheel.
    """

    def __init__(
        self,
        hwnd: int,
        *,
        first_frame_timeout_s: float = 5.0,
        stale_timeout_s: float = 5.0,
        close_timeout_s: float = 5.0,
        capture_factory: Optional[Callable[..., object]] = None,
    ):
        if hwnd <= 0:
            raise ValueError("hwnd must be a positive window handle")
        if (
            first_frame_timeout_s <= 0
            or stale_timeout_s <= 0
            or close_timeout_s <= 0
        ):
            raise ValueError("capture timeouts must be positive")

        self.hwnd = int(hwnd)
        self.first_frame_timeout_s = float(first_frame_timeout_s)
        self.stale_timeout_s = float(stale_timeout_s)
        self.close_timeout_s = float(close_timeout_s)
        self._capture_factory = capture_factory
        self._condition = threading.Condition()
        self._latest: Optional[CapturedFrame] = None
        self._last_native_timespan = None
        self._closed = False
        self._error = ""
        self._capture = None
        self._control = None

    def start(self) -> CapturedFrame:
        """Start the native session and wait for its first complete frame."""
        if self._capture is not None:
            return self.read_latest()
        if self._capture_factory is None:
            if sys.platform != "win32":
                raise WindowCaptureError(
                    "Windows Graphics Capture is available only on Windows."
                )
            try:
                from windows_capture import WindowsCapture
            except ImportError as exc:
                raise ImportError(
                    "RHEED WGC capture requires "
                    "windows-capture-interpreter==1.5.1. Install the "
                    "Windows live-capture requirements overlay."
                ) from exc
            capture_factory = WindowsCapture
        else:
            capture_factory = self._capture_factory

        capture = capture_factory(
            cursor_capture=False,
            draw_border=False,
            secondary_window=False,
            monitor_index=None,
            window_name=None,
            window_hwnd=self.hwnd,
        )

        @capture.event
        def on_frame_arrived(frame, _capture_control):
            try:
                # windows-capture-interpreter 1.5.1 may invoke the callback
                # twice for one native frame when row pitch is padded. Its
                # timespan identifies the native frame; discard only an exact
                # consecutive duplicate. Fakes/older versions without the
                # attribute retain callback-count behavior.
                native_timespan = getattr(frame, "timespan", None)
                with self._condition:
                    if (
                        native_timespan is not None
                        and native_timespan == self._last_native_timespan
                    ):
                        return
                    if native_timespan is not None:
                        self._last_native_timespan = native_timespan
                image = bgra_to_rgb(frame.frame_buffer)
                captured_ns = time.monotonic_ns()
                captured_utc = (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
                with self._condition:
                    self._latest = CapturedFrame(
                        image=image,
                        captured_at_utc=captured_utc,
                        captured_monotonic_ns=captured_ns,
                        sequence=next(_CAPTURE_SEQUENCE),
                        source_hwnd=self.hwnd,
                        width=image.shape[1],
                        height=image.shape[0],
                    )
                    self._condition.notify_all()
            except Exception as exc:  # callback errors must reach the worker
                with self._condition:
                    self._error = f"WGC frame conversion failed: {exc}"
                    self._condition.notify_all()

        @capture.event
        def on_closed():
            with self._condition:
                self._closed = True
                self._error = "kSA Live Video window closed."
                self._condition.notify_all()

        self._capture = capture
        try:
            self._control = capture.start_free_threaded()
        except Exception:
            self._capture = None
            raise

        return self._wait_for_first_frame()

    def _wait_for_first_frame(self) -> CapturedFrame:
        deadline = time.monotonic() + self.first_frame_timeout_s
        with self._condition:
            while self._latest is None and not self._closed and not self._error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

            if self._error:
                raise WindowCaptureError(self._error)
            if self._latest is None:
                raise WindowCaptureTimeout(
                    "WGC produced no first frame within "
                    f"{self.first_frame_timeout_s:.1f}s. Ensure the detached "
                    "kSA Live Video window is open and not minimized."
                )
            return self._latest

    def read_latest(self) -> CapturedFrame:
        """Return the latest atomic frame, rejecting closed or stale sessions."""
        with self._condition:
            if self._error:
                raise WindowCaptureError(self._error)
            if self._closed:
                raise WindowCaptureError("WGC capture session is closed.")
            latest = self._latest

        if latest is None:
            return self._wait_for_first_frame()
        age_s = latest.age_ms() / 1000.0
        if age_s > self.stale_timeout_s:
            raise WindowCaptureTimeout(
                f"WGC frame stream is stale ({age_s:.1f}s without a new "
                "frame). Restore the kSA Live Video window and reconnect."
            )
        return latest

    def close(self) -> None:
        """Request native shutdown and wake any waiting reader."""
        control = self._control
        self._control = None
        self._capture = None
        if control is not None:
            try:
                control.stop()
            except Exception as exc:
                log.warning("WGC capture stop failed: %s", exc)
            wait = getattr(control, "wait", None)
            if callable(wait):
                wait_done = threading.Event()

                def _wait_for_native_shutdown():
                    try:
                        wait()
                    except Exception as exc:
                        log.warning("WGC capture wait failed: %s", exc)
                    finally:
                        wait_done.set()

                waiter = threading.Thread(
                    target=_wait_for_native_shutdown,
                    name="WGCControlWait",
                    daemon=True,
                )
                waiter.start()
                if not wait_done.wait(self.close_timeout_s):
                    log.warning(
                        "WGC native capture did not finish within %.1fs",
                        self.close_timeout_s,
                    )
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed
