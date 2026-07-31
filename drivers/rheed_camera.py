"""
RHEED camera drivers — abstract interface + concrete implementations.

Two acquisition modes:
  1. VmbCamera: Direct vmbpy access to Allied Vision Manta G-033B (GigE)
  2. ScreenGrabCamera: Captures kSA 400 window via screen grab (fallback)

Classifier2 does ``.convert('L')`` on every input frame — the model is
grayscale. Both driver paths therefore aim for a **grayscale-equivalent
L channel**. VmbCamera achieves this by writing the raw intensity into
all three RGB channels ``(I, I, I)`` so ``L = 0.299·I + 0.587·I + 0.114·I
= I``; ScreenGrabCamera does it by capturing kSA's BGW false-color LUT,
which averages to the same L (see ``docs/ksa_palette_classifier_input.md``
+ ``path_3_grayscale_decoupling_design.md``).

The historical preference for ScreenGrabCamera (Classifier2 trained on
kSA screenshots) is retained as a fallback path, but VmbCamera is the
direct-read future — no screengrab UI contamination (see Jun 15 test
where a kSA tooltip appeared inside a captured RHEED frame).
"""

import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np

from drivers.window_capture import CapturedFrame, WindowsGraphicsCapture

log = logging.getLogger(__name__)


def _configure_rheed_user32_argtypes() -> None:
    """Declare 64-bit-safe signatures for RHEED-specific user32 calls."""
    import sys

    if sys.platform != "win32":
        return
    import ctypes
    import ctypes.wintypes

    from drivers.ocr import configure_user32_argtypes

    configure_user32_argtypes()
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL
    user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
    user32.IsIconic.restype = ctypes.wintypes.BOOL


class FrameNotYetAvailableError(RuntimeError):
    """The driver is connected and streaming but has not yet produced a frame.

    Distinct from "camera not connected" (which is a real failure) and from
    other RuntimeErrors surfaced by the underlying SDK. RheedCameraWorker
    should treat this as a transient — no frame produced yet, retry on next
    poll — rather than as a driver fault worth surfacing to the grower.
    """


class RheedCamera(ABC):
    """Abstract RHEED frame source."""

    @abstractmethod
    def connect(self) -> None:
        """Open connection to the camera / capture source."""

    @abstractmethod
    def read_frame(self) -> np.ndarray:
        """Return the latest frame as an RGB uint8 numpy array (H, W, 3)."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the camera / capture source."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether the source is currently available."""


class VmbCamera(RheedCamera):
    """
    Direct access to an Allied Vision camera (Manta G-033B) via the vmbpy SDK.

    Requires an exclusive camera lock — cannot run while kSA 400 holds the
    camera. Frames are palette-mapped to kSA's BGW false-color LUT via
    ``gui.ksa_palette.KSA_BGW_PALETTE`` (byte-verified against 200 training
    BMPs) so direct-Vimba output is visually and distributionally identical
    to kSA screengrab training data. Pass ``apply_palette=False`` for plain
    ``(I, I, I)`` grayscale output.

    Acquisition uses a **streaming-callback** pattern, not per-call triggering.
    With ``TriggerMode='On'`` the camera produces frames only into an active
    streaming pipeline — a bare ``get_frame()`` registers no consumer, so
    every call times out (confirmed on Bulbasaur, May 8 2026). Instead a
    background thread opens the camera, starts streaming with a frame handler,
    and software-triggers at ``trigger_hz``. The handler keeps the single most
    recent frame in a thread-safe slot; ``read_frame()`` returns a copy of it,
    so the poll-based ``RheedCameraWorker`` consumes this driver unchanged.
    """

    # Time to wait in connect() for the streaming thread to become ready
    # (or fail). 10s is generous — Manta G-033B on Bulbasaur typically
    # completes VmbSystem + camera open + start_streaming in <2s. Tunable
    # if a slower camera shows up.
    CONNECT_TIMEOUT_S = 10.0

    # Time to wait in disconnect() for the streaming thread to exit.
    # 5s covers the worst case where TriggerSoftware.run() is mid-call.
    DISCONNECT_TIMEOUT_S = 5.0

    # Consecutive trigger/frame failures before the stream loop backs off
    # to a slower retry cadence. Prevents busy-looping at trigger_hz when
    # the camera has silently gone offline.
    MAX_CONSECUTIVE_TRIGGER_FAILS = 5

    # Backoff wait after MAX_CONSECUTIVE_TRIGGER_FAILS raises — enough
    # to let a transient recover (network hiccup, kSA cycling the port)
    # without spinning the CPU.
    TRIGGER_BACKOFF_S = 2.0

    def __init__(
        self,
        camera_index: int = 0,
        trigger_hz: float = 1.0,
        bit_depth: int = 12,
        apply_palette: bool = True,
    ):
        self._camera_index = camera_index
        self._trigger_hz = trigger_hz
        self._bit_depth = bit_depth
        # Fixed normalization denominator (4095 for 12-bit Manta G-033B).
        # Used to map raw uint16 ADC samples into the uint8 range while
        # keeping the scale factor consistent across frames — per-frame
        # max-normalization (the obvious alternative) would drift the
        # scale and produce synthetic change scores between frames whose
        # raw pixel values are identical but max intensity differs.
        self._max_value = (1 << bit_depth) - 1
        self._apply_palette = apply_palette
        self._connected = False

        # Streaming state. The stream thread owns every vmbpy call for a
        # connect cycle; connect() blocks on _ready_event until it is
        # streaming (or has failed). read_frame() reads _latest_frame under
        # _frame_lock — the handler writes it under the same lock.
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        # Any exception that terminated the stream thread. Populated by
        # _stream_loop's ``except`` block, read by ``read_frame`` so
        # mid-session stream death surfaces to the caller with its real
        # cause instead of a bare "Camera not connected."
        self._stream_error: Optional[Exception] = None
        # Guards writes to _stream_error, _last_frame_error, and reads
        # of the same by external threads. Independent of _frame_lock so
        # the SDK callback doesn't wait on read_frame.
        self._error_lock = threading.Lock()
        self._last_frame_error: Optional[str] = None

    def connect(self) -> None:
        # Fail fast with a clear message if the SDK is missing, before
        # spawning the thread (the thread re-imports — module is cached).
        try:
            import vmbpy  # noqa: F401
        except ImportError:
            raise ImportError(
                "vmbpy not installed. Install the Allied Vision Vimba X SDK "
                "or use ScreenGrabCamera for screen-scrape mode."
            )

        # Fresh primitives per connect cycle — a connect after a previous
        # disconnect must not observe stale event state.
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        with self._error_lock:
            self._stream_error = None
            self._last_frame_error = None
        with self._frame_lock:
            self._latest_frame = None

        self._stream_thread = threading.Thread(
            target=self._stream_loop, name="VmbCameraStream", daemon=True,
        )
        self._stream_thread.start()

        # Block until the thread is streaming, has reported a setup failure,
        # or hangs past the timeout.
        if not self._ready_event.wait(timeout=self.CONNECT_TIMEOUT_S):
            self._stop_event.set()
            raise RuntimeError(
                f"Vimba camera connect timed out — no response from the "
                f"streaming thread within {self.CONNECT_TIMEOUT_S:.0f} s."
            )
        with self._error_lock:
            setup_error = self._stream_error
        if setup_error is not None:
            raise setup_error
        self._connected = True
        log.info(
            "VmbCamera connected: camera_index=%d, trigger_hz=%.2f, bit_depth=%d",
            self._camera_index, self._trigger_hz, self._bit_depth,
        )

    def _stream_loop(self) -> None:
        """Background thread — owns every vmbpy call for one connect cycle.

        Opens VmbSystem + camera, configures the software trigger, starts
        streaming with _frame_handler, then triggers at trigger_hz until
        disconnect() sets _stop_event. Keeping all vmbpy calls on this one
        thread respects the SDK's thread affinity; the ``with`` blocks
        release the camera even on error, so kSA — or a reconnect — can
        re-acquire it.

        Trigger failures don't kill the loop — a bounded consecutive-fail
        counter forces a short backoff so a transient (network hiccup,
        kSA cycling the port) has room to recover without spinning the CPU.
        """
        try:
            from vmbpy import VmbSystem

            with VmbSystem.get_instance() as vmb:
                cams = vmb.get_all_cameras()
                if not cams:
                    raise RuntimeError(
                        "No Allied Vision cameras found — confirm kSA 400 is "
                        "fully closed (it holds the camera exclusively)."
                    )
                with cams[self._camera_index] as cam:
                    # Software trigger → deterministic, controlled frame rate.
                    cam.TriggerSource.set("Software")
                    cam.TriggerSelector.set("FrameStart")
                    cam.TriggerMode.set("On")
                    cam.AcquisitionMode.set("Continuous")

                    cam.start_streaming(self._frame_handler)
                    try:
                        # connect() unblocks here — the pipeline is live.
                        self._ready_event.set()
                        period = 1.0 / self._trigger_hz
                        consecutive_trigger_fails = 0
                        while not self._stop_event.is_set():
                            try:
                                cam.TriggerSoftware.run()
                                consecutive_trigger_fails = 0
                            except Exception as exc:  # noqa: BLE001
                                consecutive_trigger_fails += 1
                                with self._error_lock:
                                    self._last_frame_error = (
                                        f"TriggerSoftware.run: {exc}"
                                    )
                                if (
                                    consecutive_trigger_fails
                                    >= self.MAX_CONSECUTIVE_TRIGGER_FAILS
                                ):
                                    log.warning(
                                        "VmbCamera: %d consecutive trigger fails; "
                                        "backing off %.1fs before retry",
                                        consecutive_trigger_fails,
                                        self.TRIGGER_BACKOFF_S,
                                    )
                                    self._stop_event.wait(self.TRIGGER_BACKOFF_S)
                                    consecutive_trigger_fails = 0
                                    continue
                            # Interruptible pacing — disconnect() wakes this
                            # at once instead of after a full period.
                            self._stop_event.wait(period)
                    finally:
                        cam.stop_streaming()
        except Exception as exc:
            with self._error_lock:
                self._stream_error = exc
            log.error("VmbCamera stream loop exited on error: %s", exc)
        finally:
            self._connected = False
            # Unblock connect() even if setup failed before the set() above.
            self._ready_event.set()

    def _frame_handler(self, cam, _stream, frame) -> None:
        """vmbpy streaming callback — runs on the SDK's handler thread.

        Copies the frame out, recycles the buffer, converts to the GUI's
        RGB-uint8 contract, and stores it as the latest frame. A failure
        here must not kill the stream: a bad frame is recorded and skipped.
        """
        try:
            try:
                # as_numpy_ndarray() is a view into the frame buffer — copy
                # it out BEFORE queue_frame() hands the buffer back.
                img = frame.as_numpy_ndarray().copy().squeeze()
            finally:
                # Always recycle: a leaked buffer shrinks the pool and,
                # once it is exhausted, silently halts capture.
                cam.queue_frame(frame)
            rgb = self._to_rgb_uint8(img)
            with self._frame_lock:
                self._latest_frame = rgb
        except Exception as exc:  # noqa: BLE001
            # Record but don't kill the stream — one bad frame shouldn't
            # take down the whole session. Guarded by _error_lock so
            # concurrent reads from read_frame see a consistent value.
            with self._error_lock:
                self._last_frame_error = f"frame handler: {exc}"

    def _to_rgb_uint8(self, img: np.ndarray) -> np.ndarray:
        """Map a raw camera frame to the GUI's RGB-uint8 (H, W, 3) contract.

        Normalizes with a FIXED bit-depth denominator (not per-frame max):
        per-frame max-normalization would drift the scale between frames
        with identical raw pixels but different peak intensity, injecting
        synthetic change scores into the std-of-|diff| detector.

        When ``apply_palette=True`` (default) the uint8 intensity is passed
        through the kSA BGW LUT from ``gui.ksa_palette`` so frames look and
        classify identically to kSA screengrab training data. When False,
        intensity is written into all three channels ``(I, I, I)``.
        """
        if img.dtype != np.uint8:
            img = (
                img.astype(np.float32) / self._max_value * 255.0
            ).clip(0, 255).astype(np.uint8)
        if img.ndim == 2:
            if self._apply_palette:
                from gui.ksa_palette import KSA_BGW_PALETTE  # noqa: PLC0415
                return KSA_BGW_PALETTE[img]
            return np.stack([img, img, img], axis=-1)
        return img

    def read_frame(self) -> np.ndarray:
        """Return the most recent streamed frame as RGB uint8 (H, W, 3).

        Non-blocking — returns whatever the streaming thread captured last.

        Raises:
            RuntimeError: the stream thread died with an underlying cause
                (surfaced from _stream_error), OR the driver was never
                connected. The error message names the real cause so the
                worker's state.error carries useful information.
            FrameNotYetAvailableError: the driver is connected and streaming
                but no frame has arrived yet. Worker should treat as transient
                and retry on next poll rather than surfacing to the grower.
        """
        # Surface a stream-thread crash first — if _stream_error is set,
        # _connected has already been flipped to False by _stream_loop's
        # finally block, so checking this before the connected guard means
        # the caller sees "why the camera stopped," not just "not connected."
        with self._error_lock:
            stream_err = self._stream_error
        if stream_err is not None:
            raise RuntimeError(f"Vimba stream terminated: {stream_err}") from stream_err

        if not self._connected:
            raise RuntimeError("Camera not connected.")
        with self._frame_lock:
            latest = self._latest_frame
        if latest is None:
            raise FrameNotYetAvailableError(
                "Vimba camera is streaming but no frame has arrived yet — "
                "expected within one trigger period after connect."
            )
        return latest.copy()

    def disconnect(self) -> None:
        self._connected = False
        self._stop_event.set()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=self.DISCONNECT_TIMEOUT_S)
            if self._stream_thread.is_alive():
                log.warning(
                    "VmbCamera stream thread did not exit within %.1fs; "
                    "leaking as daemon.", self.DISCONNECT_TIMEOUT_S,
                )
            self._stream_thread = None
        with self._frame_lock:
            self._latest_frame = None

    @property
    def connected(self) -> bool:
        return self._connected


class ScreenGrabCamera(RheedCamera):
    """
    Captures frames from the detached kSA Live Video window.

    Historically the primary path for ML classification because Classifier2
    was trained on kSA false-color screenshots. Now serves as a fallback
    while ``VmbCamera`` (direct-read) is being lab-validated — see
    ``docs/path_a_vimba_integration_plan.md`` and the ``(I, I, I)`` palette
    fix that makes both paths produce equivalent L-channel input to the
    classifier.

    The default ``wgc`` backend captures a detached top-level window by HWND,
    independent of desktop z-order. It is Windows-only and fails closed if
    the target closes, is minimized, or stops producing frames. ``mss`` is
    retained only as an explicit legacy diagnostic backend and still reads
    visible desktop pixels.

    Chrome cropping removes the kSA title bar / menu / toolbar (above)
    and status bar (below) so the classifier sees only the RHEED image
    region.

    Parameters
    ----------
    window_title : str
        Substring to search for in window titles (default "Live Video").
        Matches either the detached top-level Live Video window or the
        MDI child pane inside the kSA 400 main frame.
    crop_chrome : bool
        If True, crop kSA window chrome (title bar + menu + toolbar
        above, status bar below) so the classifier sees only the RHEED
        image area. Default True.
    chrome_top_px : int
        Pixels to crop from the top. Default 75 — measured 2026-04-25
        (title=29 + menu=22 + toolbar=24 = 75). Verify with
        :meth:`visualize_crop` on Bulbasaur if the value drifts under
        different DPI / theme.
    chrome_bottom_px : int
        Pixels to crop from the bottom. Default 30 — kSA status bar
        ("Exposure: ... NUM") plus bottom window border.
    """

    # Consecutive read_frame failures before we mark disconnected so the
    # worker's reconnect path can trigger. Symmetric with ExactusSerialPyrometer
    # and VmbCamera — the entire driver fleet uses the same convention.
    MAX_CONSECUTIVE_FAILS = 5

    def __init__(
        self,
        window_title: str = "Live Video",
        crop_chrome: bool = True,
        chrome_top_px: int = 75,
        chrome_bottom_px: int = 30,
        backend: str = "wgc",
        first_frame_timeout_s: float = 5.0,
        stale_timeout_s: float = 5.0,
        capture_factory=None,
    ):
        if backend not in {"wgc", "mss"}:
            raise ValueError("backend must be 'wgc' or 'mss'")
        self._window_title = window_title
        self._backend = backend
        self._connected = False
        self._capture_method: Optional[str] = None
        self._crop_chrome = crop_chrome
        self._chrome_top_px = chrome_top_px
        self._chrome_bottom_px = chrome_bottom_px
        self._first_frame_timeout_s = first_frame_timeout_s
        self._stale_timeout_s = stale_timeout_s
        self._capture_factory = capture_factory
        self._capture_session: Optional[WindowsGraphicsCapture] = None
        self._last_capture: Optional[CapturedFrame] = None
        self._consecutive_fails = 0
        # One-shot warning gate for the cross-platform (whole-monitor) grab.
        # Set on first invocation so we log only once per session — repeat
        # logging at 1Hz would drown the log.
        self._warned_cross_platform = False

    @classmethod
    def legacy_mss(cls, **kwargs) -> "ScreenGrabCamera":
        """Create the explicit legacy framebuffer-capture implementation."""
        return cls(backend="mss", **kwargs)

    @staticmethod
    def _find_detached_live_video_window(
        search_term: str = "Live Video",
    ) -> int:
        """Return only a visible, detached top-level Live Video HWND."""
        import ctypes
        import ctypes.wintypes

        _configure_rheed_user32_argtypes()
        search_lower = search_term.lower()
        user32 = ctypes.windll.user32
        main_pid = ctypes.wintypes.DWORD(0)
        detached_hwnd = ctypes.c_void_p(0)

        def _get_title(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value

        @ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        def _find_main(hwnd, _lp):
            title = _get_title(hwnd).lower()
            if title.startswith("ksa 400"):
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(main_pid))
                return False
            return True

        user32.EnumWindows(_find_main, 0)
        if not main_pid.value:
            return 0

        @ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        def _find_detached(hwnd, _lp):
            title = _get_title(hwnd).lower()
            candidate_pid = ctypes.wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(candidate_pid))
            if (
                search_lower in title
                and not title.startswith("ksa 400")
                and user32.IsWindowVisible(hwnd)
                and candidate_pid.value == main_pid.value
            ):
                detached_hwnd.value = hwnd
                return False
            return True

        user32.EnumWindows(_find_detached, 0)
        return int(detached_hwnd.value or 0)

    @staticmethod
    def _find_live_video_window(search_term: str = "Live Video") -> int:
        """Find the kSA Live Video window handle.

        Search strategy (in priority order):
        1. Top-level window containing *search_term* but NOT starting with
           "kSA 400" — this is the detached Live Video window (when the kSA
           option "Keep Live Video inside application" is unchecked).
        2. Child window of the main kSA 400 frame containing *search_term*
           — this is the MDI child pane (default kSA layout).
        3. The main kSA 400 window itself as a fallback.

        Returns the HWND (int) or 0 if kSA 400 is not running.
        """
        import ctypes
        import ctypes.wintypes

        _configure_rheed_user32_argtypes()
        search_lower = search_term.lower()
        user32 = ctypes.windll.user32

        def _get_title(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value

        # --- Step 1: scan ALL top-level windows ---
        main_hwnd = ctypes.c_void_p(0)
        detached_hwnd = ctypes.c_void_p(0)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_toplevel(hwnd, _lp):
            title = _get_title(hwnd).lower()
            if not title:
                return True
            # Detached Live Video window (priority 1)
            if search_lower in title and not title.startswith("ksa 400"):
                detached_hwnd.value = hwnd
                return False  # found best match, stop
            # Main kSA 400 frame
            if title.startswith("ksa 400") and not main_hwnd.value:
                main_hwnd.value = hwnd
            return True

        user32.EnumWindows(_enum_toplevel, 0)

        # Priority 1: detached top-level Live Video window
        if detached_hwnd.value:
            return int(detached_hwnd.value)

        if not main_hwnd.value:
            return 0

        # --- Step 2: search children of main kSA window ---
        child_hwnd = ctypes.c_void_p(0)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_children(hwnd, _lp):
            title = _get_title(hwnd).lower()
            if search_lower in title:
                child_hwnd.value = hwnd
                return False
            return True

        user32.EnumChildWindows(
            ctypes.wintypes.HWND(int(main_hwnd.value)), _enum_children, 0
        )

        if child_hwnd.value:
            return int(child_hwnd.value)

        # No Live Video pane found — do NOT silently fall back to the
        # main kSA window. That path would capture the entire kSA UI
        # (menus, toolbars, MDI panes) and feed it to the classifier,
        # which would still return a label — silently wrong. Better to
        # return 0 and let the caller surface the real failure.
        log.warning(
            "ScreenGrabCamera: found kSA main window but no Live Video "
            "child/detached pane. Ensure kSA 400's Live Video window is "
            "open (View menu). Refusing to fall back to the main frame."
        )
        return 0

    def connect(self) -> None:
        """Start the selected capture backend and verify a first frame."""
        self._setup()
        self._connected = True
        self._consecutive_fails = 0
        log.info(
            "ScreenGrabCamera connected: window='%s', capture=%s, "
            "crop_chrome=%s (top=%d, bottom=%d)",
            self._window_title, self._capture_method,
            self._crop_chrome, self._chrome_top_px, self._chrome_bottom_px,
        )

    def _setup(self) -> None:
        """Initialize WGC or the explicitly requested legacy mss backend."""
        import sys

        if self._backend == "wgc":
            if sys.platform != "win32" and self._capture_factory is None:
                raise RuntimeError(
                    "RHEED WGC mode requires Windows. Select dummy/vimba "
                    "for development or screengrab_mss for legacy diagnostics."
                )
            hwnd = (
                1
                if self._capture_factory is not None and sys.platform != "win32"
                else self._find_detached_live_video_window(self._window_title)
            )
            if not hwnd:
                raise RuntimeError(
                    "Detached kSA Live Video window not found. In kSA 400, "
                    "disable 'Keep Live Video inside application', open Live "
                    "Video as its own window, then reconnect."
                )
            if sys.platform == "win32":
                import ctypes

                _configure_rheed_user32_argtypes()
                if ctypes.windll.user32.IsIconic(hwnd):
                    raise RuntimeError(
                        "Detached kSA Live Video window is minimized. Restore "
                        "it before connecting WGC."
                    )
            session = WindowsGraphicsCapture(
                hwnd,
                first_frame_timeout_s=self._first_frame_timeout_s,
                stale_timeout_s=self._stale_timeout_s,
                capture_factory=self._capture_factory,
            )
            try:
                first = session.start()
            except Exception:
                session.close()
                raise
            self._capture_session = session
            self._last_capture = first.with_image(
                self._crop_chrome_pixels(first.image)
            )
            self._capture_method = "wgc"
            return

        try:
            import mss  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "mss required for legacy screen capture: pip install mss"
            ) from exc
        self._capture_method = "mss"

    def read_frame(self) -> np.ndarray:
        if not self._connected:
            raise RuntimeError("ScreenGrabCamera not connected.")

        import sys

        try:
            if self._backend == "wgc":
                self._assert_wgc_window_available()
                if self._capture_session is None:
                    raise RuntimeError("WGC capture session is not initialized.")
                sample = self._capture_session.read_latest()
                cropped = self._crop_chrome_pixels(sample.image)
                self._last_capture = sample.with_image(cropped)
            else:
                if sys.platform == "win32":
                    frame = self._grab_win32()
                    hwnd = self._find_live_video_window(self._window_title)
                else:
                    frame = self._grab_cross_platform()
                    hwnd = 0
                cropped = self._crop_chrome_pixels(frame)
                self._last_capture = self._make_legacy_capture(cropped, hwnd)
        except RuntimeError:
            self._register_failure()
            raise
        self._consecutive_fails = 0
        return cropped

    def _assert_wgc_window_available(self) -> None:
        """Fail before returning a cached frame when the HWND is unavailable."""
        import sys

        if sys.platform != "win32":
            return
        import ctypes

        _configure_rheed_user32_argtypes()
        if self._capture_session is None:
            raise RuntimeError("WGC capture session is not initialized.")
        hwnd = self._capture_session.hwnd
        if not ctypes.windll.user32.IsWindow(hwnd):
            raise RuntimeError("Detached kSA Live Video window was closed.")
        if ctypes.windll.user32.IsIconic(hwnd):
            raise RuntimeError(
                "Detached kSA Live Video window is minimized. RHEED capture "
                "stopped; restore it and reconnect."
            )

    @staticmethod
    def _make_legacy_capture(frame: np.ndarray, hwnd: int) -> CapturedFrame:
        """Attach software-receipt provenance to an explicit mss frame."""
        import time
        from datetime import datetime, timezone

        now_ns = time.monotonic_ns()
        captured_utc = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        height, width = frame.shape[:2]
        return CapturedFrame(
            image=frame,
            captured_at_utc=captured_utc,
            captured_monotonic_ns=now_ns,
            sequence=now_ns,
            source_hwnd=int(hwnd),
            width=width,
            height=height,
            backend="mss",
        )

    def _register_failure(self) -> None:
        """Track consecutive failures — mark disconnected past the threshold.

        Symmetric with ExactusSerialPyrometer and VmbCamera: after
        MAX_CONSECUTIVE_FAILS raises in a row, flip ``_connected`` to
        False so the worker's reconnect path can trigger (e.g., grower
        reopened the kSA Live Video window and we should re-verify).
        """
        self._consecutive_fails += 1
        if self._consecutive_fails >= self.MAX_CONSECUTIVE_FAILS:
            log.warning(
                "ScreenGrabCamera: %d consecutive frame failures; marking "
                "disconnected so the worker can retry connect. Typical "
                "cause: kSA Live Video window closed or minimized.",
                self._consecutive_fails,
            )
            self._connected = False

    def _crop_chrome_pixels(self, frame: np.ndarray) -> np.ndarray:
        """Crop title/menu/toolbar above and status bar below the RHEED image.

        Defensive: if the configured crop bounds are invalid for this frame
        (e.g., window much smaller than expected), returns the frame
        unchanged rather than producing an empty array.
        """
        if not self._crop_chrome:
            return frame
        h = frame.shape[0]
        top = max(0, self._chrome_top_px)
        bottom = h - max(0, self._chrome_bottom_px)
        if bottom <= top:
            return frame
        return frame[top:bottom, :]

    def _grab_win32(self) -> np.ndarray:
        """Capture kSA Live Video window on Windows using win32 + mss."""
        import ctypes
        import ctypes.wintypes
        import mss

        _configure_rheed_user32_argtypes()
        # Find the Live Video window (detached top-level or MDI child).
        # If _find_live_video_window returns 0, raise cleanly rather than
        # silently falling back to the main kSA frame.
        hwnd = self._find_live_video_window(self._window_title)
        if not hwnd:
            raise RuntimeError(
                "kSA Live Video window not found. Open View → Live Video "
                "in kSA 400, then rearm the session."
            )

        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

        monitor = {
            "left": rect.left,
            "top": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        }

        with mss.mss() as sct:
            screenshot = sct.grab(monitor)
        return self._bgra_to_rgb(np.array(screenshot))

    def _grab_cross_platform(self) -> np.ndarray:
        """Capture whole primary monitor via mss — fallback for dev sessions.

        This path returns the ENTIRE primary display, not just the kSA
        window (no ctypes.windll access outside Windows). Fine for Mac-
        side GUI development, wrong for production data collection.
        The one-shot warning at first call keeps the log signal-to-noise
        high — repeat spam at 1Hz would drown other messages.
        """
        import mss

        if not self._warned_cross_platform:
            self._warned_cross_platform = True
            log.warning(
                "ScreenGrabCamera cross-platform grab is capturing the "
                "ENTIRE primary monitor. This is dev-only fallback — for "
                "production data collection, run on Bulbasaur with the "
                "win32 path so we crop to the kSA window."
            )

        with mss.mss() as sct:
            monitor = sct.monitors[1]  # index 1 = primary display
            screenshot = sct.grab(monitor)
        return self._bgra_to_rgb(np.array(screenshot))

    @staticmethod
    def _bgra_to_rgb(bgra: np.ndarray) -> np.ndarray:
        """Convert an mss BGRA screenshot array to RGB uint8 (H, W, 3).

        mss returns BGRA; the GUI's contract is RGB. Drops the alpha
        channel and reverses the color axis in one pass.
        """
        return bgra[:, :, :3][:, :, ::-1]

    def get_info(self) -> dict:
        """Return static config summary for state.device_info / debug logs."""
        import sys
        return {
            "name": "ScreenGrabCamera",
            "platform": sys.platform,
            "capture_method": self._capture_method or "not_connected",
            "backend": self._backend,
            "window_title": self._window_title,
            "crop_chrome": self._crop_chrome,
            "chrome_top_px": self._chrome_top_px,
            "chrome_bottom_px": self._chrome_bottom_px,
        }

    @property
    def last_capture(self) -> Optional[CapturedFrame]:
        """Latest returned frame and its atomic capture provenance."""
        return self._last_capture

    def visualize_crop(self, frame: np.ndarray) -> np.ndarray:
        """Overlay the crop boundaries on a captured frame for calibration QA.

        The default ``chrome_top_px=75`` / ``chrome_bottom_px=30`` were
        measured on a specific kSA screenshot (2026-04-25). If Bulbasaur's
        DPI or theme changes, the crop can silently mis-align — call this
        method on a fresh grab to visually verify the crop still targets
        the correct kSA image region.

        Returns a copy of ``frame`` with two horizontal green lines drawn
        at the crop boundaries and a small text label. Save with PIL for
        visual review::

            from PIL import Image
            cam = ScreenGrabCamera()
            cam.connect()
            raw = cam._grab_win32()  # pre-crop
            annotated = cam.visualize_crop(raw)
            Image.fromarray(annotated).save("crop_qa.png")
        """
        vis = frame.copy()
        h = vis.shape[0]
        top = max(0, min(self._chrome_top_px, h - 1))
        bottom_offset = max(0, min(self._chrome_bottom_px, h - 1))
        bottom = h - bottom_offset
        # Green horizontal lines — 2 px thick for visibility on 12-bit data.
        vis[top : top + 2, :, 0] = 0
        vis[top : top + 2, :, 1] = 255
        vis[top : top + 2, :, 2] = 0
        vis[bottom - 2 : bottom, :, 0] = 0
        vis[bottom - 2 : bottom, :, 1] = 255
        vis[bottom - 2 : bottom, :, 2] = 0
        return vis

    def disconnect(self) -> None:
        if self._capture_session is not None:
            self._capture_session.close()
            self._capture_session = None
        self._connected = False
        self._last_capture = None
        self._consecutive_fails = 0
        self._warned_cross_platform = False

    @property
    def connected(self) -> bool:
        return self._connected


class DummyCamera(RheedCamera):
    """Test camera that plays back real experimental RHEED images.

    Shows unmodified BMP frames — the live image IS the truth.
    Calibration warps the basis images (right pane) to match.
    """

    PRESETS: dict[str, str] = {
        "dummy":      "1x1",
        "dummy_c6x2": "c6x2",
        "dummy_tw":   "Twinned2x1",
    }
    DEFAULT_PRESET = "dummy"

    def __init__(self, width: int = 656, height: int = 492,
                 preset: str | None = None):
        self._width = width
        self._height = height
        self._connected = False
        self._frame_count = 0
        self._images: list = []
        self._data_class = self.PRESETS.get(preset or self.DEFAULT_PRESET,
                                            self.PRESETS[self.DEFAULT_PRESET])

    def _find_data_dir(self) -> str | None:
        """Locate the RHEEDClassify data directory for this preset's class."""
        from pathlib import Path
        candidates = [
            Path(f"D:/AI4MBE/RHEEDClassify/data/STO_ideal_{self._data_class}"),
            Path(f"../RHEEDClassify/data/STO_ideal_{self._data_class}"),
        ]
        for p in candidates:
            if p.is_dir():
                return str(p)
        return None

    def _load_images(self):
        """Pre-load and downsample real experimental RHEED frames."""
        if self._images:
            return
        from PIL import Image
        from scripts.equalizer_ui import PROCESS_WH
        pw, ph = PROCESS_WH

        data_dir = self._find_data_dir()
        if data_dir is None:
            return

        import glob
        files = sorted(glob.glob(f"{data_dir}/*.bmp"))[:20]  # first 20 frames
        for fpath in files:
            try:
                img = Image.open(fpath).convert("L")
                img = img.resize((pw, ph), Image.Resampling.BILINEAR)
                self._images.append(np.asarray(img, dtype=np.float32))
            except Exception:
                continue

    def connect(self) -> None:
        self._connected = True
        self._frame_count = 0
        self._load_images()

    def read_frame(self) -> np.ndarray:
        if not self._connected:
            raise RuntimeError("Dummy camera not connected.")
        self._load_images()

        h, w = self._height, self._width
        frame = np.zeros((h, w, 3), dtype=np.uint8)

        if not self._images:
            # Fallback: simple Gaussian spot if no data found.
            from scripts.equalizer_ui import PROCESS_WH
            pw, ph = PROCESS_WH
            fallback = np.zeros((ph, pw), dtype=np.float32)
            cx, cy = pw // 2, ph // 2
            yg, xg = np.ogrid[:ph, :pw]
            r2 = (xg - cx) ** 2 + (yg - cy) ** 2
            fallback += 200 * np.exp(-r2 / (2 * 50**2))
        else:
            # Show a single stable image (first frame, always the same).
            fallback = self._images[0].copy()

        # Show raw image — no transform. Left pane is truth.
        blend = fallback.astype(np.float32)

        blend = np.clip(blend, 0, 255).astype(np.float32)

        # Upsample to camera resolution.
        from PIL import Image
        u8 = np.asarray(blend, dtype=np.uint8)
        up = np.array(Image.fromarray(u8).resize((int(w), int(h)), Image.BILINEAR))

        frame[:, :, 1] = up  # green channel
        frame[:, :, 2] = np.clip(up.astype(np.int16) // 3, 0, 255).astype(np.uint8)
        self._frame_count += 1
        return frame

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected
