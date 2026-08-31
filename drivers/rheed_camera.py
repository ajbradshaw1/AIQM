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

import hashlib
import json
import logging
import math
import sys
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from drivers.window_capture import CapturedFrame, WindowsGraphicsCapture

log = logging.getLogger(__name__)


_DEFAULT_WINDOW_DPI = 96


def _window_dpi_identity(
    hwnd: int,
    dpi_provider: Optional[Callable[[int], int]] = None,
) -> str:
    """Return a stable, fail-safe DPI identity for one top-level window.

    Production uses ``GetDpiForWindow``.  The injected provider keeps the
    behavior deterministic in platform-independent tests.  An unavailable,
    failing, or zero-returning API is represented explicitly as a 96-DPI
    fallback instead of being confused with a successful native reading.
    """
    dpi = 0
    source = "fallback"
    try:
        if dpi_provider is not None:
            dpi = int(dpi_provider(int(hwnd)))
            source = "getdpiforwindow"
        else:
            import sys

            if sys.platform == "win32" and int(hwnd) > 0:
                import ctypes
                import ctypes.wintypes

                get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow")
                get_dpi.argtypes = [ctypes.wintypes.HWND]
                get_dpi.restype = ctypes.wintypes.UINT
                dpi = int(get_dpi(ctypes.wintypes.HWND(int(hwnd))))
                source = "getdpiforwindow"
    except Exception:
        dpi = 0
        source = "fallback"
    if dpi <= 0:
        dpi = _DEFAULT_WINDOW_DPI
        source = "fallback"
    return f"dpi-{source}-{dpi}"


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


class _Unreadable:
    """Sentinel: a camera feature exists but its accessor raised.

    Distinct from ``None``, which means the accessor is absent. A feature
    that cannot be read is not a feature that is absent — the first leaves
    a safety precondition unproven, the second means there is no
    precondition to prove. See VmbCamera._optional_feature_call.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unreadable>"


UNREADABLE = _Unreadable()


class _Absent:
    """Sentinel: the camera genuinely does not expose this feature.

    Distinct from ``UNREADABLE``, which means the node exists (or its status
    could not be determined) and the lookup itself failed. Absence is a fact
    about the camera and may be tolerated for optional features; an
    unreadable lookup is an unproven precondition and must not be.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


ABSENT = _Absent()


class RestorationFailedError(RuntimeError):
    """A rollback could not be proven, so the camera is in an unknown state.

    Raised only when the driver has mutated the camera and then failed to put
    it back. The original failure is always chained as ``__cause__`` — losing
    it would leave an operator with a restoration error and no idea what
    provoked it.
    """


# Process-wide lease registry: which camera each live setup thread owns.
#
# The orphan guard cannot live on a VmbCamera instance. The GUI discards a
# failed worker and builds a new driver object, so an instance-scoped check
# sees a pristine object while the previous instance's daemon thread is still
# blocked inside the SDK holding the same physical camera. Reproduced: a
# second VmbCamera connected and streamed against the same camera index while
# the first one's setup thread was still alive.
#
# Keyed by camera index until a stable device identity is known, then re-keyed
# to that identity. Released only in the owning setup thread's finally.
_CAMERA_LEASES: dict = {}
_CAMERA_LEASE_LOCK = threading.Lock()


def _acquire_camera_lease(key, owner: str):
    """Claim a camera, returning a token the owning thread releases with.

    The lease is acquired on the CALLING thread (connect()) but released on
    the SETUP thread, so ownership cannot be identified by
    ``threading.current_thread()`` at both ends — doing so silently never
    matched, and the lease was never released. The token is the identity.

    ``thread`` is filled in by _bind_camera_lease once the thread object
    exists; until then the lease counts as held, so a concurrent connect
    cannot slip through the gap between claim and start.
    """
    token = object()
    with _CAMERA_LEASE_LOCK:
        holder = _CAMERA_LEASES.get(key)
        if holder is not None:
            thread = holder["thread"]
            if thread is None or thread.is_alive():
                raise RuntimeError(
                    f"Camera {key!r} is already held by a live setup thread "
                    f"({holder['owner']}). Refusing a second connection to "
                    "the same camera — wait for it to drain, or power-cycle "
                    "if it never does."
                )
        _CAMERA_LEASES[key] = {"thread": None, "owner": owner, "token": token}
    return (key, token)


def _bind_camera_lease(lease, thread) -> None:
    """Attach the setup thread to a lease claimed by its parent."""
    if lease is None:
        return
    key, token = lease
    with _CAMERA_LEASE_LOCK:
        holder = _CAMERA_LEASES.get(key)
        if holder is not None and holder["token"] is token:
            holder["thread"] = thread


def _reset_camera_leases() -> None:
    """Drop every lease. TEST-HARNESS ONLY.

    Represents "the process ended and the cameras went away". Production code
    must never call this: it exists so a test harness that tears down its fake
    SDK between cases does not inherit a lease held by a thread the previous
    case deliberately left blocked.
    """
    with _CAMERA_LEASE_LOCK:
        _CAMERA_LEASES.clear()


def _release_camera_lease(lease) -> None:
    """Release a lease, but only if this token still owns it."""
    if lease is None:
        return
    key, token = lease
    with _CAMERA_LEASE_LOCK:
        holder = _CAMERA_LEASES.get(key)
        if holder is not None and holder["token"] is token:
            del _CAMERA_LEASES[key]


class _HardwareTransaction:
    """Every camera mutation in one ARM, rolled back together on failure.

    ARM is a single hardware transaction: access mode, exposure, four trigger
    features and start_streaming. Treating those as independent steps meant a
    failure late in the sequence left earlier mutations applied — a camera
    sitting at a requested exposure the GUI had reported as a failed ARM.

    Mutations are recorded BEFORE the setter runs, because a setter can apply
    the value and then block or raise. Rollback restores in reverse order and
    verifies each one; anything unproven raises RestorationFailedError with
    the original failure chained.
    """

    def __init__(self, verify):
        self._entries: list = []
        self._verify = verify
        self.streaming_attempted = False
        self.committed = False

    def record(self, feature_name: str, feature, original) -> None:
        self._entries.append((feature_name, feature, original))

    def rollback(self, cam, cause: BaseException) -> None:
        """Undo everything, newest first. Never raises the rollback's own
        bookkeeping errors in place of ``cause``."""
        failures = []
        if self.streaming_attempted:
            # May have partially succeeded before raising, so this is
            # attempted unconditionally rather than only on clean success.
            try:
                cam.stop_streaming()
            except Exception as exc:  # noqa: BLE001
                log.debug("stop_streaming during rollback: %s", exc)
        for feature_name, feature, original in reversed(self._entries):
            try:
                feature.set(original)
                if not self._verify(feature, original):
                    failures.append(feature_name)
            except Exception as exc:  # noqa: BLE001
                log.debug("restore of %s raised: %s", feature_name, exc)
                failures.append(feature_name)
        self._entries.clear()
        if failures:
            raise RestorationFailedError(
                "The camera was modified during a failed ARM and could not be "
                f"restored: {', '.join(failures)}. POWER-CYCLE THE CAMERA "
                "before the next session; its current configuration is not "
                "what either the GUI or the stored user set describes."
            ) from cause


class FrameNotYetAvailableError(RuntimeError):
    """The driver is connected and streaming but has not yet produced a frame.

    Distinct from "camera not connected" (which is a real failure) and from
    other RuntimeErrors surfaced by the underlying SDK. RheedCameraWorker
    should treat this as a transient — no frame produced yet, retry on next
    poll — rather than as a driver fault worth surfacing to the grower.
    """


class _AccessDenialError(Exception):
    """Internal: signals that a VmbCamera open attempt hit access denial.

    Wraps a vmbpy.VmbCameraError raised at set_access_mode() or __enter__().
    Only the outer `_stream_loop` catches this — it's what tells the
    auto-mode orchestrator "the SDK refused the open" vs "something else
    broke inside the streaming session." Downstream VmbPy errors (feature
    config, streaming) propagate raw to the outer except handler and land
    in _stream_error unchanged.
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

    Two-mode access via ``access_mode``:

    * ``"full"`` (or successful ``"auto"``): exclusive control. Driver
      configures the trigger pipeline and software-triggers at
      ``trigger_hz``. Cannot coexist with kSA 400 holding the camera.
    * ``"read"`` (or ``"auto"`` after Full is denied): passive consumer.
      Another process (typically kSA + Vimba multicast) owns acquisition;
      the driver just subscribes to the stream. Requires camera multicast
      enabled in the persistent user set (see Task #187).

    In ``"auto"`` mode the driver tries Full first; if the SDK refuses
    (kSA holds the exclusive lock), it retries once with Read. Once the
    active access mode is negotiated at connect time it is frozen — no
    auto-upgrade Read→Full mid-session (a "helpful" upgrade while growers
    are actively using kSA in Full would silently steal their stream).
    A fresh ``disconnect()`` + ``connect()`` cycle re-tries Full first.

    Frames are palette-mapped to kSA's BGW false-color LUT via
    ``gui.ksa_palette.KSA_BGW_PALETTE`` (byte-verified against 200 training
    BMPs) so direct-Vimba output is visually and distributionally identical
    to kSA screengrab training data. Pass ``apply_palette=False`` for plain
    ``(I, I, I)`` grayscale output.

    Acquisition uses a **streaming-callback** pattern, not per-call triggering.
    With ``TriggerMode='On'`` the camera produces frames only into an active
    streaming pipeline — a bare ``get_frame()`` registers no consumer, so
    every call times out (confirmed on Bulbasaur, May 8 2026). Instead a
    background thread opens the camera, starts streaming with a frame handler,
    and (in Full mode) software-triggers at ``trigger_hz``. The handler keeps
    the single most recent frame in a thread-safe slot; ``read_frame()``
    returns a copy of it, so the poll-based ``RheedCameraWorker`` consumes
    this driver unchanged regardless of the negotiated access mode.
    """

    # Time to wait in connect() for the streaming thread to become ready
    # (or fail). Generous 45s to accommodate slow GigE discovery on
    # machines with multiple Vimba transport-layer providers registered
    # (e.g. old Vimba + Vimba X both installed → get_all_cameras() walks
    # every interface, adding 15-20s). Camera open itself is <2s.
    CONNECT_TIMEOUT_S = 45.0

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

    # Valid access_mode values. Kept as a class-level constant so tests
    # can introspect the accepted set without importing the module twice.
    _ACCESS_MODE_CHOICES = ("auto", "full", "read")
    _EXPOSURE_FEATURE_CANDIDATES = (
        "ExposureTimeAbs",  # confirmed on Ch-MBE Manta G-033B
        "ExposureTime",     # SFNC spelling on newer Allied Vision cameras
    )
    # Preserve at least 10% timing headroom between integration time and the
    # software-trigger period. Camera transport/readout overhead makes an
    # exposure equal to the entire period unsafe even if 1/exposure looks OK.
    _MAX_EXPOSURE_PERIOD_FRACTION = 0.90

    def __init__(
        self,
        camera_index: int = 0,
        trigger_hz: float = 1.0,
        bit_depth: int = 12,
        apply_palette: bool = True,
        access_mode: str = "auto",
        exposure_us: Optional[float] = None,
    ):
        if access_mode not in self._ACCESS_MODE_CHOICES:
            raise ValueError(
                f"access_mode must be one of {self._ACCESS_MODE_CHOICES}, "
                f"got {access_mode!r}"
            )
        # Fail closed on non-finite and boolean inputs BEFORE any arithmetic.
        # `nan <= 0` is False, so NaN passed the old positivity test; inf gave
        # a zero-length trigger period (a spin loop); and bool is an int
        # subclass, so True became a 1.0 Hz trigger nobody asked for.
        if not self._usable_number(trigger_hz):
            raise ValueError(
                f"trigger_hz must be a finite positive number, got "
                f"{trigger_hz!r}"
            )
        trigger_hz = float(trigger_hz)
        # A finite subnormal survives the check above but overflows on
        # inversion: 1.0 / 5e-324 is inf, which later reaches
        # Event.wait(inf) and raises from inside the stream thread — far from
        # the value that caused it. Validate the DERIVED period, which is what
        # the trigger loop and the exposure ceiling actually use.
        period_s = 1.0 / trigger_hz
        if not (
            math.isfinite(period_s)
            and 0.0 < period_s <= threading.TIMEOUT_MAX
        ):
            # threading.TIMEOUT_MAX, not merely "finite": 1e-308 Hz inverts to
            # a finite ~1e308 s period that Event.wait() rejects with
            # OverflowError from inside the stream thread, far from the value
            # that caused it. The loop must be able to actually wait on it.
            raise ValueError(
                f"trigger_hz={trigger_hz!r} yields an unusable trigger period "
                f"({period_s!r} s); it must be positive and no greater than "
                f"threading.TIMEOUT_MAX ({threading.TIMEOUT_MAX:g} s)"
            )
        if exposure_us is not None:
            if not self._usable_number(exposure_us):
                raise ValueError(
                    f"exposure_us must be a finite positive number when "
                    f"provided, got {exposure_us!r}"
                )
            exposure_us = float(exposure_us)
            safe_max_us = (
                1_000_000.0 / trigger_hz
                * self._MAX_EXPOSURE_PERIOD_FRACTION
            )
            if exposure_us > safe_max_us:
                raise ValueError(
                    f"exposure_us={exposure_us:.0f} is too long for "
                    f"trigger_hz={trigger_hz:.3g}; use <= {safe_max_us:.0f} us "
                    "to preserve 10% acquisition headroom"
                )
        self._camera_index = camera_index
        self._trigger_hz = trigger_hz
        self._requested_exposure_us = exposure_us
        self._exposure_us: Optional[float] = None
        # Read-only, point-in-time camera feature snapshot captured after a
        # successful setup and before streaming starts.  It deliberately
        # survives disconnect so session_metadata.json can still include it
        # after the worker has stopped.  A fresh connect clears it first so a
        # failed reconnect can never expose settings from an older cycle.
        self._sensor_settings_lock = threading.Lock()
        self._sensor_settings_at_connect: dict = {}
        self._bit_depth = bit_depth
        # Fixed normalization denominator (4095 for 12-bit Manta G-033B).
        # Used to map raw uint16 ADC samples into the uint8 range while
        # keeping the scale factor consistent across frames — per-frame
        # max-normalization (the obvious alternative) would drift the
        # scale and produce synthetic change scores between frames whose
        # raw pixel values are identical but max intensity differs.
        self._max_value = (1 << bit_depth) - 1
        self._apply_palette = apply_palette
        # User-requested access mode (validated above). "auto" triggers
        # Full-first-then-Read-fallback in _stream_loop; "full"/"read"
        # skip the fallback and just try that mode once.
        self._requested_access_mode = access_mode
        # Negotiated access mode after a successful open, populated by
        # _run_one_session. "" before connect / after disconnect.
        self._active_access_mode = ""
        self._connected = False

        # Streaming state. The stream thread owns every vmbpy call for a
        # connect cycle; connect() blocks on _ready_event until it is
        # streaming (or has failed). read_frame() reads _latest_frame under
        # _frame_lock — the handler writes it under the same lock.
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Serialises the terminal decision (committed vs cancelled) so a
        # connect() timeout and a setup-thread commit cannot both win.
        self._lifecycle_lock = threading.Lock()
        self._lease_key = None
        # Set when a possibly-running stream could not be stopped.
        self._unknown_hardware_state = None
        self._ready_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_sequence = 0
        self._last_delivered_sequence = 0
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

        # LIVE EXPOSURE REQUESTS.
        #
        # The class contract is that the stream thread owns every vmbpy call
        # for a connect cycle, so a grower's mid-session exposure change
        # cannot be written from the GUI thread — it would race the trigger
        # loop and the SDK frame callback. The request therefore crosses
        # threads as data: the caller parks a value here, and
        # _trigger_and_idle_loop applies it between triggers, where it already
        # holds `cam` inside `with cam:`.
        #
        # Its own lock, not _error_lock or _frame_lock: an SDK exposure write
        # can block for hundreds of milliseconds and must never stall
        # read_frame() or the frame handler.
        self._exposure_request_lock = threading.Lock()
        self._pending_exposure_us: Optional[float] = None
        # Increments on every CONFIRMED live change. Consumers use it to tell
        # "re-applied the same value" from "no change", and the intensity
        # monitor uses it to break its trend at the discontinuity.
        self._exposure_generation = 0
        self._last_exposure_error = ""

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

        if self._unknown_hardware_state is not None:
            raise RuntimeError(
                "Refusing to arm: the camera was left in an unknown state — "
                f"{self._unknown_hardware_state}. POWER-CYCLE the camera, "
                "then restart the Growth Monitor."
            )

        # An orphaned setup thread from a previous cycle may still be blocked
        # inside an SDK call. Starting a second one would give two threads the
        # same camera, and the older one can still reach its exposure write
        # once the block clears. Refuse until it has actually drained.
        previous = self._stream_thread
        if previous is not None and previous.is_alive():
            raise RuntimeError(
                "A previous Vimba setup thread is still running (it did not "
                f"exit within {self.DISCONNECT_TIMEOUT_S:.0f}s of disconnect). "
                "Refusing to connect a second thread to the same camera — "
                "wait for it to drain, or power-cycle if it never does."
            )

        # Fresh primitives per connect cycle — a connect after a previous
        # disconnect must not observe stale event state.
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        with self._error_lock:
            self._stream_error = None
            self._last_frame_error = None
        self._exposure_us = None
        # A live request parked against the PREVIOUS cycle must not be applied
        # to a freshly opened camera — the grower asked for it under different
        # hardware state, and the new cycle has its own ARM-time exposure.
        with self._exposure_request_lock:
            self._pending_exposure_us = None
            self._last_exposure_error = ""
        with self._sensor_settings_lock:
            self._sensor_settings_at_connect = {}
        with self._frame_lock:
            self._latest_frame = None
            self._latest_frame_sequence = 0
            self._last_delivered_sequence = 0

        # Process-level, not instance-level: the GUI discards a failed worker
        # and builds a fresh driver object, so an instance check sees a clean
        # slate while the previous daemon still holds the physical camera.
        self._lease_key = _acquire_camera_lease(
            self._camera_index, f"VmbCamera(index={self._camera_index})",
        )
        self._stream_thread = threading.Thread(
            target=self._stream_loop, name="VmbCameraStream", daemon=True,
        )
        _bind_camera_lease(self._lease_key, self._stream_thread)
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

    @staticmethod
    def _optional_feature(owner, name: str):
        """Look a feature up without trusting getattr's AttributeError guard.

        vmbpy resolves features through ``__getattr__`` against the camera's
        live GenICam node map, so an unavailable or unreadable node can
        surface as an SDK-specific error rather than the AttributeError that
        ``getattr(..., default)`` suppresses. That matters for the exposure
        candidate search: an unguarded lookup of ``ExposureTimeAbs`` on a
        camera that raises for it would abort before ``ExposureTime`` — the
        valid alternative — is ever tried, and the camera would be reported
        as having no exposure feature at all.
        """
        try:
            return getattr(owner, name, None)
        except Exception:  # noqa: BLE001 — vmbpy raises SDK-specific types
            log.debug("Feature lookup for %r raised; treating as absent", name)
            return None

    @staticmethod
    def _lookup_feature(owner, name: str):
        """Three-outcome feature lookup: the feature, ``ABSENT``, or ``UNREADABLE``.

        ``_optional_feature`` deliberately collapses a raising lookup into
        "absent". That is correct for the exposure candidate search, where the
        fallback is simply the next spelling — but it is fail-OPEN for a safety
        precondition. An SDK error while locating ``ExposureAuto`` would be
        reported as "this camera has no auto-exposure", and the caller's
        ``is not None`` check would pass without ever proving the auto loop is
        off. The write would then race it.

        This is the fourth time this codebase has conflated absent with
        unreadable in a vmbpy accessor; the distinction is the whole point of
        the sentinels. Safety-critical callers must treat ``UNREADABLE`` as a
        refusal and may tolerate ``ABSENT`` only where absence is genuinely
        benign — i.e. where there is no precondition left to prove.
        """
        try:
            return getattr(owner, name)
        except AttributeError:
            return ABSENT
        except Exception:  # noqa: BLE001 — vmbpy raises SDK-specific types
            log.debug("Feature lookup for %r raised; unreadable", name)
            return UNREADABLE

    @staticmethod
    def _accessor_call(feature, method_name: str):
        """Call an accessor with three honest outcomes.

        * accessor attribute genuinely missing -> ``ABSENT``
        * present but unusable, or it raised   -> ``UNREADABLE``
        * otherwise                            -> the value

        "Present but unusable" covers ``get = None`` and any non-callable. The
        older helper returned its ``default`` for those, which is how a feature
        that exists but cannot answer was read as "no such feature" — and
        absence is tolerated for the optional ones. A safety precondition then
        passed without ever being evaluated.

        Only genuine ABSENCE may be tolerated, and only where the caller has
        nothing left to prove. Anything else is an unproven precondition.
        """
        try:
            method = getattr(feature, method_name)
        except AttributeError:
            return ABSENT
        except Exception:  # noqa: BLE001 — vmbpy raises SDK-specific types
            log.debug("Accessor lookup %r raised; unreadable", method_name)
            return UNREADABLE
        if method is None or not callable(method):
            log.debug("Accessor %r is present but not callable; unreadable",
                      method_name)
            return UNREADABLE
        try:
            return method()
        except Exception:  # noqa: BLE001 — vmbpy raises SDK-specific types
            log.debug("Accessor %r raised; unreadable", method_name)
            return UNREADABLE

    # Every COMPLETE representation that counts as auto-exposure disabled.
    #
    # Matched whole, never by suffix and never after discarding an arbitrary
    # prefix. Splitting on "." and keeping the tail accepted "Bogus.Off" and
    # "ExposureAuto.Not.Off" — strings that say nothing about this camera's
    # ExposureAuto node, or say the opposite. The accepted forms are the ones
    # vmbpy actually produces for an enum feature: the bare entry name, the
    # feature-qualified name, and the EnumEntry repr wrapping either.
    _EXPOSURE_AUTO_OFF = "off"

    @classmethod
    def _is_exposure_auto_off(cls, value) -> bool:
        """Whether an ExposureAuto read proves the auto loop is disabled.

        Compares the WHOLE normalised representation against a canonical set.
        A suffix test accepted "NotOff" and "TurnedOff"; discarding a dotted
        prefix additionally accepted "Bogus.Off" and "ExposureAuto.Not.Off".
        Neither says this camera's auto-exposure is off, and one says the
        opposite.

        vmbpy enum reads surface as the entry name, a feature-qualified name,
        or an EnumEntry repr; a driver may also hand back an object exposing
        ``get_name()``/``name``. All are normalised to a complete string and
        matched in full.
        """
        if value is None:
            return False
        # vmbpy's EnumFeature.get() returns an EnumEntry whose string form is
        # the bare entry name, so this is exact equality against "off" and
        # nothing else. Successive attempts here each widened it — a suffix
        # ("NotOff"), a dotted tail ("Bogus.Off"), a set of qualified
        # spellings the SDK does not emit, then quote stripping that admitted
        # "'Off'" — and every widening added a form that can WRONGLY authorise
        # a hardware write. Whitespace is the only normalisation applied.
        return str(value).strip().casefold() == cls._EXPOSURE_AUTO_OFF

    class _CancelledError(RuntimeError):
        """The connect cycle was abandoned while setup was still running."""

    def _raise_if_cancelled(self, where: str) -> None:
        """Abort setup if connect() gave up or disconnect() ran.

        The setup thread can outlive both. connect() returns after
        CONNECT_TIMEOUT_S whether or not the thread is finished, and
        disconnect() only joins for DISCONNECT_TIMEOUT_S — so a thread blocked
        inside an SDK call could wake afterwards and carry on configuring a
        camera the GUI had already reported as failed and released. Observed:
        connect() timed out, disconnect() returned with no write, the blocked
        accessor was then released, and the orphan wrote 250000 us and started
        streaming.

        Checked at every point where the next statement would touch the camera
        or publish state derived from it.
        """
        if self._stop_event.is_set():
            raise self._CancelledError(
                f"Vimba setup cancelled ({where}); the connect cycle was "
                "abandoned before this step ran."
            )

    @staticmethod
    def _finite_real(value) -> bool:
        """True for a finite, non-boolean real number (zero and negatives OK).

        ``bool`` is excluded because it is an ``int`` subclass, so a device
        returning True for a range endpoint would otherwise be read as 1.0.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(float(value))

    @classmethod
    def _matches(cls, observed: float, expected: float) -> bool:
        """Tight equality for an exposure the device was told to apply.

        Deliberately NOT a tolerance of one whole increment. The request has
        already been snapped onto the device's own grid, so the device should
        land exactly on it; allowing a full increment of slack accepted a
        camera that quantised somewhere else entirely and reported a different
        exposure than the one the GUI confirms to the grower.

        The remaining slack is float-representation noise only. A device that
        rounds off a grid it never declared will fail this and be restored —
        which is the intended fail-closed outcome, not a bug: the exposure the
        frames were taken at would otherwise be unknown.
        """
        return math.isclose(observed, expected, rel_tol=1e-9, abs_tol=1e-6)

    @staticmethod
    def _usable_number(value) -> bool:
        """True for a finite, positive, non-boolean real number.

        ``bool`` is excluded deliberately: it is a subclass of ``int``, so
        ``isinstance(True, (int, float))`` passes and a camera returning True
        for a frame-rate limit would otherwise be compared as 1.0 fps.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(float(value)) and float(value) > 0.0

    @classmethod
    def _optional_feature_call(cls, feature, method_name: str, default=None):
        """Call an optional read accessor, distinguishing absent from broken.

        Three outcomes, and the difference between the last two is the whole
        point:

        * accessor missing        -> ``default``    (feature cannot answer)
        * accessor raised         -> ``UNREADABLE`` (feature exists, read failed)
        * otherwise               -> the value

        Collapsing "raised" into "absent" would silently skip validation that
        the caller believes it performed. A camera whose ``ExposureAuto`` read
        fails is not a camera without auto-exposure — it is a camera whose
        auto-exposure state is unknown, and a manual write must refuse rather
        than race the auto loop. Safety-critical callers therefore check for
        ``UNREADABLE`` explicitly; only genuinely optional reads (increment)
        treat it as absent.

        The post-``set`` readback deliberately does **not** go through here —
        there a raising ``get()`` must propagate so the original exposure is
        restored.
        """
        # Deliberately NOT routed through _optional_feature: that helper maps
        # a raising lookup to "absent", which is right for the camera-level
        # candidate search (fall through to the next spelling) and wrong
        # here. An accessor whose lookup raises is a failed read, not a
        # missing one, and must fail closed like any other failed read.
        try:
            method = getattr(feature, method_name, None)
        except Exception:  # noqa: BLE001 — vmbpy raises SDK-specific types
            log.debug("Accessor lookup %r raised; unreadable", method_name)
            return UNREADABLE
        if method is None or not callable(method):
            return default
        try:
            return method()
        except Exception:  # noqa: BLE001 — vmbpy raises SDK-specific types
            log.debug("Feature accessor %r raised; unreadable", method_name)
            return UNREADABLE

    def _configure_exposure(self, cam, mode: str, txn=None) -> None:
        """Read or apply the requested manual exposure before streaming.

        A requested write is fail-closed: it requires Full access,
        ExposureAuto=Off, a writable numeric feature, valid device range, an
        exact readback, and an achievable frame-rate limit. If validation
        after ``set`` fails, the original value is restored before raising.
        No user-set save command is issued, so the change remains volatile.
        """
        feature_name = next(
            (
                name for name in self._EXPOSURE_FEATURE_CANDIDATES
                if self._optional_feature(cam, name) is not None
            ),
            None,
        )
        if feature_name is None:
            if self._requested_exposure_us is None:
                return
            raise RuntimeError(
                "Manual exposure requested, but neither ExposureTimeAbs nor "
                "ExposureTime exists on this camera"
            )

        self._raise_if_cancelled("before reading the current exposure")
        feature = self._optional_feature(cam, feature_name)
        current = self._accessor_call(feature, "get")
        # PROVENANCE GATE. This value is published as CameraState.exposure_us
        # and lands in session metadata as the confirmed exposure, so an
        # unusable read must stay None rather than becoming a recorded number.
        # `isinstance(current, (int, float))` admitted NaN and inf verbatim and
        # silently turned True into 1.0 us.
        if self._usable_number(current):
            self._exposure_us = float(current)
        else:
            self._exposure_us = None
            if current is not ABSENT and current is not UNREADABLE:
                log.warning(
                    "%s reported an unusable exposure (%r); recording no "
                    "confirmed exposure for this arm rather than publishing "
                    "an invalid one", feature_name, current,
                )
            else:
                log.warning(
                    "%s could not be read; recording no confirmed exposure "
                    "for this arm", feature_name,
                )
        if self._requested_exposure_us is None:
            # Keep-current: no write, and provenance stays honest above.
            return
        self._apply_exposure_write(
            cam,
            mode,
            feature_name,
            feature,
            current,
            float(self._requested_exposure_us),
            txn=txn,
        )

    def _apply_exposure_write(
        self,
        cam,
        mode: str,
        feature_name: str,
        feature,
        current,
        requested_us: float,
        *,
        txn=None,
    ) -> float:
        """Validate and write one exposure value; return the confirmed readback.

        Shared by two callers with different lifetimes:

        * ``_configure_exposure`` at ARM, which passes the connect-cycle
          ``txn`` so a failure anywhere in the setup sequence rolls the
          exposure back with every other mutation.
        * ``_service_exposure_request`` while streaming, which passes no
          transaction. A grower's deliberate mid-session change is not part of
          the ARM transaction and must not be reverted by a later DISARM —
          the same policy already applied to a committed ARM exposure.

        Every gate is fail-closed: an unreadable answer is a refusal, never an
        assumption. ``current`` is the pre-write value and the restore target,
        so it must already be a usable number by the time it reaches here.
        """
        if mode != "full":
            raise RuntimeError(
                "Manual exposure requires Full camera access. Close kSA or "
                "Vimba X Viewer, then disarm and arm again; the GUI refused "
                "to pretend the requested exposure was applied in Read mode."
            )

        # A camera with no ExposureAuto feature has no auto loop to race, so
        # genuine ABSENCE is tolerated. A lookup that RAISED proves nothing —
        # it must refuse, not be mistaken for absence.
        auto_feature = self._lookup_feature(cam, "ExposureAuto")
        if auto_feature is UNREADABLE:
            raise RuntimeError(
                "Manual exposure requires ExposureAuto=Off, but the "
                "ExposureAuto feature could not be looked up — refusing "
                "rather than assuming the camera has no auto-exposure loop"
            )
        if auto_feature is not ABSENT:
            # The node exists, so its state MUST be readable and demonstrably
            # Off. An accessor that is missing, None, non-callable, raising, or
            # answers None tells us nothing about the auto loop — and "nothing"
            # is not "off".
            auto_value = self._accessor_call(auto_feature, "get")
            if auto_value is UNREADABLE or auto_value is ABSENT:
                raise RuntimeError(
                    "Manual exposure requires ExposureAuto=Off, but the "
                    "ExposureAuto feature exists and could not report its "
                    "state — refusing rather than racing a possibly active "
                    "auto-exposure loop"
                )
            if auto_value is None:
                raise RuntimeError(
                    "ExposureAuto exists but reported no state; refusing "
                    "rather than assuming the auto-exposure loop is off"
                )
            if not self._is_exposure_auto_off(auto_value):
                raise RuntimeError(
                    f"Manual exposure requires ExposureAuto=Off; camera "
                    f"reports {auto_value!r}"
                )

        # Require a genuine two-Boolean tuple with BOTH bits explicitly True.
        # `bool(access[1])` authorised the write for the string "False", the
        # int 1, and any arbitrary object — every non-empty value is truthy,
        # so a camera that reported its access mode in an unexpected shape was
        # read as "writable". Readability matters too: the readback check is
        # meaningless if the feature cannot be read back.
        access = self._accessor_call(feature, "get_access_mode")
        if not (
            isinstance(access, tuple)
            and len(access) == 2
            and all(isinstance(bit, bool) for bit in access)
        ):
            raise RuntimeError(
                f"{feature_name} reported an unusable access mode "
                f"({access!r}); expected a (readable, writable) pair of "
                "booleans — refusing the write"
            )
        readable, writable = access
        if not (readable and writable):
            raise RuntimeError(
                f"{feature_name} is not readable+writable in Full camera "
                f"access (readable={readable}, writable={writable}). Close "
                "kSA or the Vimba X Viewer and arm again."
            )
        # The original is the restore target. A NaN/inf/boolean here means we
        # could never put the camera back, so writing at all would be a
        # one-way change to a grower's camera.
        if not self._usable_number(current):
            raise RuntimeError(
                f"Could not read a usable original {feature_name} "
                f"({current!r}); refusing to write an exposure that could not "
                "then be restored"
            )
        current = float(current)

        requested = float(requested_us)
        bounds = self._accessor_call(feature, "get_range")
        if bounds is UNREADABLE or bounds is ABSENT:
            raise RuntimeError(
                f"{feature_name} exposes a range that could not be read — "
                "refusing the write rather than skipping range validation"
            )
        # A valid two-number range is REQUIRED, not optional. Falling through
        # on a missing or malformed range would perform the write with no
        # bounds check at all — the caller believes the device range was
        # validated, so silently skipping it is the same fail-open the
        # sentinels exist to prevent.
        if not (
            isinstance(bounds, tuple)
            and len(bounds) == 2
            and all(self._finite_real(b) for b in bounds)
        ):
            raise RuntimeError(
                f"{feature_name} did not report a usable [min, max] range "
                f"(got {bounds!r}) — refusing the write rather than skipping "
                "range validation"
            )
        low, high = float(bounds[0]), float(bounds[1])
        if not low <= high:
            raise RuntimeError(
                f"{feature_name} reported an inverted range "
                f"[{low:.0f}, {high:.0f}] us — refusing the write"
            )
        if not low <= requested <= high:
            raise RuntimeError(
                f"Requested exposure {requested:.0f} us is outside the "
                f"camera range [{low:.0f}, {high:.0f}] us"
            )
        # The ORIGINAL must also lie in the reported range, checked before any
        # write. If it does not, the two facts are inconsistent — and the value
        # we would hand back during restoration is one the device has just
        # declared it cannot accept, so the restore could not succeed. Refusing
        # here keeps the camera untouched instead of discovering that after the
        # write has already landed.
        if not low <= current <= high:
            raise RuntimeError(
                f"The camera's current {feature_name} ({current:.0f} us) is "
                f"outside its own reported range [{low:.0f}, {high:.0f}] us, "
                "so it could not be restored after a failed write — refusing "
                "before touching the camera"
            )
        # Increment is optional only in the sense that a camera may not HAVE
        # one. An accessor that exists and fails is a different thing: it means
        # the quantisation grid is unknown, so the value written might not be
        # representable. Downgrading that to "no increment" is the same
        # fail-open pattern as the safety features, so it refuses.
        increment = self._accessor_call(feature, "get_increment")
        if increment is UNREADABLE:
            raise RuntimeError(
                f"{feature_name} exposes an increment that could not be read "
                "— refusing rather than writing a value that may not sit on "
                "the device's quantisation grid"
            )
        if increment is ABSENT:
            increment = None
        if increment is not None and not self._usable_number(increment):
            raise RuntimeError(
                f"{feature_name} reported an unusable increment "
                f"({increment!r}) — refusing the write"
            )
        if increment is not None:
            requested = low + round((requested - low) / increment) * increment
            # Re-check AFTER snapping. The earlier bounds test validated the
            # value the grower asked for, not the value actually about to be
            # written: rounding to the nearest grid point can step past `high`.
            # With range (100, 160) and increment 100, a request of 160 snaps
            # to 200 — outside the device's own declared range, written anyway,
            # and then "confirmed" by a tolerance wide enough to accept it.
            if not self._finite_real(requested):
                raise RuntimeError(
                    f"{feature_name} quantisation produced an unusable value "
                    f"({requested!r}) — refusing the write"
                )
            if not low <= requested <= high:
                raise RuntimeError(
                    f"Quantising to the {increment:g} us grid moved the "
                    f"request to {requested:.0f} us, outside the camera range "
                    f"[{low:.0f}, {high:.0f}] us — refusing rather than "
                    "writing an out-of-range value"
                )
            # The timing ceiling must be re-checked too. It was validated in
            # __init__ against the value the grower asked for, not the value
            # about to be written. With a 1 Hz trigger, a 800000 us request on
            # a 500000 us grid snaps to 1000100 us — past the 900000 us
            # headroom ceiling AND past the whole trigger period. Without
            # AcquisitionFrameRateLimit to catch it afterwards, that lands on
            # the camera and quietly over-triggers it.
            safe_max_us = (
                1_000_000.0 / self._trigger_hz
                * self._MAX_EXPOSURE_PERIOD_FRACTION
            )
            if requested > safe_max_us:
                raise RuntimeError(
                    f"Quantising to the {increment:g} us grid moved the "
                    f"request to {requested:.0f} us, beyond the "
                    f"{safe_max_us:.0f} us ceiling that preserves 10% "
                    f"headroom at {self._trigger_hz:.3g} Hz — refusing"
                )

        # Validate the setter BEFORE assuming a write can be attempted, so a
        # missing/non-callable set() is a refusal rather than an AttributeError
        # that bypasses restoration entirely.
        setter = getattr(feature, "set", None)
        if setter is None or not callable(setter):
            raise RuntimeError(
                f"{feature_name} exposes no usable set() — refusing the write"
            )

        # Marked BEFORE invoking, not after it returns. A device can apply the
        # value and then raise because its acknowledgement was lost; with the
        # flag set afterwards that path skipped restoration and left the camera
        # altered. Treating "possibly applied" as "applied" costs one redundant
        # restore in the never-applied case and prevents a silent
        # reconfiguration in the other.
        # Last check before touching the camera. Past this point a
        # cancellation must run verified restoration, not simply return.
        self._raise_if_cancelled("immediately before the exposure write")

        # Registered BEFORE invoking, so a setter that applies and then
        # blocks or raises is still rolled back. From here the exposure is
        # PROVISIONAL: it is only final once streaming commits.
        if txn is not None:
            txn.record(feature_name, feature, current)

        wrote = True
        try:
            setter(requested)
            # Inside the try: a cancellation detected here has to reach the
            # restoration path, because the write may already have applied.
            self._raise_if_cancelled("immediately after the exposure write")
            # Validate BEFORE arithmetic. float(nan) succeeds and then every
            # comparison against it is False, so `abs(nan - requested) >
            # tolerance` silently accepted a NaN readback and stored it as the
            # confirmed exposure.
            raw_readback = feature.get()
            if not self._usable_number(raw_readback):
                raise RuntimeError(
                    f"{feature_name} readback is not a usable number "
                    f"({raw_readback!r}) — the applied exposure cannot be "
                    "confirmed"
                )
            readback = float(raw_readback)
            if not self._matches(readback, requested):
                raise RuntimeError(
                    f"{feature_name} readback {readback:.3f} us does not "
                    f"match the requested {requested:.3f} us"
                )

            limit_feature = self._lookup_feature(
                cam, "AcquisitionFrameRateLimit",
            )
            # Genuinely ABSENT -> rely on the conservative 90%-of-period
            # ceiling enforced at construction. A lookup that RAISED is a
            # failed verification, not an absent feature: refuse and restore.
            if limit_feature is UNREADABLE:
                raise RuntimeError(
                    "AcquisitionFrameRateLimit could not be looked up, so the "
                    f"{self._trigger_hz:.3f} Hz trigger rate cannot be "
                    "confirmed achievable at the requested exposure"
                )
            if limit_feature is not ABSENT:
                # The node exists, so it MUST produce a usable number. An
                # accessor that is missing, None, non-callable, raising, or
                # answers NaN/inf/<=0 leaves the achievable rate unproven, and
                # an unproven rate is not a confirmed one.
                limit = self._accessor_call(limit_feature, "get")
                if limit is UNREADABLE or limit is ABSENT:
                    raise RuntimeError(
                        "AcquisitionFrameRateLimit exists but could not report "
                        f"a value, so the {self._trigger_hz:.3f} Hz trigger "
                        "rate cannot be confirmed achievable at the requested "
                        "exposure"
                    )
                if not self._usable_number(limit):
                    raise RuntimeError(
                        "AcquisitionFrameRateLimit reported an unusable value "
                        f"({limit!r}); the {self._trigger_hz:.3f} Hz trigger "
                        "rate cannot be confirmed achievable"
                    )
                if self._trigger_hz > float(limit):
                    raise RuntimeError(
                        f"Camera reports a {float(limit):.3f} fps limit at "
                        f"{readback:.0f} us, below the requested "
                        f"{self._trigger_hz:.3f} Hz trigger rate"
                    )
            self._raise_if_cancelled("before publishing confirmed exposure")
            self._exposure_us = readback
            log.info(
                "VmbCamera manual exposure applied: %s=%.0f us "
                "(requested %.0f us, volatile)",
                feature_name, readback, requested_us,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised below
            # With a transaction, rollback is owned there so every mutation is
            # undone in reverse order. Restoring here as well would write the
            # original twice and hide ordering bugs. The local path remains for
            # a direct call with no transaction.
            if wrote and txn is None:
                # Restoration must be VERIFIED, not merely attempted. The old
                # path set the original, read once, and stored whatever came
                # back without comparing it. A device that silently clamps both
                # the requested write and the restore would leave the camera
                # altered while the caller saw only the original error — the
                # grower's camera changed, and nothing said so.
                try:
                    feature.set(current)
                    restored = feature.get()
                except Exception as restore_exc:  # noqa: BLE001
                    log.critical(
                        "VmbCamera could not restore %s after a failed "
                        "configuration: %s. The camera is left at an "
                        "unintended exposure — POWER-CYCLE THE CAMERA.",
                        feature_name, restore_exc,
                    )
                    raise RuntimeError(
                        f"Exposure configuration failed AND {feature_name} "
                        f"could not be restored ({restore_exc}). The camera "
                        "is left at an unintended exposure — power-cycle it "
                        "before the next arm."
                    ) from exc
                if not self._usable_number(restored) or not self._matches(
                    float(restored), current,
                ):
                    log.critical(
                        "VmbCamera restore of %s did not take: expected "
                        "%.3f us, camera reports %r. POWER-CYCLE THE CAMERA.",
                        feature_name, current, restored,
                    )
                    raise RuntimeError(
                        f"Exposure configuration failed and the restore of "
                        f"{feature_name} could not be verified: expected "
                        f"{current:.3f} us, camera reports {restored!r}. The "
                        "camera is left at an unintended exposure — "
                        "power-cycle it before the next arm."
                    ) from exc
                self._exposure_us = float(restored)
            raise
        return float(self._exposure_us)


    def _stream_loop(self) -> None:
        """Background thread — owns every vmbpy call for one connect cycle.

        Opens VmbSystem + camera, then delegates to `_run_one_session`
        which negotiates the access mode (set_access_mode + __enter__),
        gate-configures the trigger pipeline, starts streaming, and runs
        the trigger/idle loop until disconnect() sets _stop_event. Keeping
        all vmbpy calls on this one thread respects the SDK's thread
        affinity; each attempt's own `with cam:` block releases the camera
        even on error, so kSA — or a reconnect — can re-acquire it.

        Auto mode: try Full first; on _AccessDenialError, retry Read.
        If Read also fails, raise a combined RuntimeError naming both
        failure modes without over-pointing at any single root cause.
        Explicit "full"/"read" modes don't retry — the sentinel error is
        unwrapped so the caller sees a normal RuntimeError.

        Trigger failures inside a session don't kill the loop — a bounded
        consecutive-fail counter in _trigger_and_idle_loop forces a short
        backoff so a transient (network hiccup, kSA cycling the port) has
        room to recover without spinning the CPU.
        """
        try:
            import vmbpy

            with vmbpy.VmbSystem.get_instance() as vmb:
                cams = vmb.get_all_cameras()
                if not cams:
                    raise RuntimeError(
                        "No Allied Vision cameras found — confirm the "
                        "camera is powered on and reachable to Vimba "
                        "(kSA-open coexistence still requires the camera "
                        "itself to be visible to the SDK)."
                    )
                cam = cams[self._camera_index]

                if self._requested_access_mode == "auto":
                    try:
                        self._run_one_session(cam, "full")
                    except _AccessDenialError as full_err:
                        log.info(
                            "VmbCamera: Full mode denied (%s); "
                            "retrying with Read.", full_err,
                        )
                        try:
                            self._run_one_session(cam, "read")
                        except _AccessDenialError as read_err:
                            raise RuntimeError(
                                "Full denied; Read also failed. Check "
                                "kSA state, camera permitted access "
                                "modes, Vimba X multicast/read-sharing "
                                "config, and Task #187. "
                                f"Full: {full_err} | Read: {read_err}"
                            ) from read_err
                else:
                    try:
                        self._run_one_session(cam, self._requested_access_mode)
                    except _AccessDenialError as e:
                        # Explicit mode: no fallback. Unwrap the sentinel
                        # so the caller sees a normal RuntimeError, not
                        # the internal marker class.
                        raise RuntimeError(str(e)) from e.__cause__
        except Exception as exc:
            with self._error_lock:
                self._stream_error = exc
            log.error("VmbCamera stream loop exited on error: %s", exc)
        finally:
            self._connected = False
            self._active_access_mode = ""
            # Released here, in the owning thread, on EVERY exit path — a
            # lease outliving its thread would lock the camera out forever.
            if self._lease_key is not None:
                _release_camera_lease(self._lease_key)
            # Unblock connect() even if setup failed before the set() above.
            self._ready_event.set()

    def _run_one_session(self, cam, mode: str) -> None:
        """Set access mode, open, configure, stream, close cleanly.

        Self-contained per-attempt lifecycle: `with cam:` scopes the entire
        open→configure→stream→close cycle. Only VmbCameraErrors from
        `set_access_mode` or `__enter__` are wrapped in _AccessDenialError
        (the outer orchestrator's fallback signal); any other exception,
        including VmbCameraErrors from feature config or streaming,
        propagates raw to the outer error handler and lands in _stream_error.

        The `past_open` flag distinguishes __enter__ failure from later
        VmbCameraErrors — see constraint 7 of the plan.
        """
        import vmbpy
        access_enum = getattr(vmbpy.AccessMode, mode.capitalize())

        # set_access_mode can block past the timeout; a cancelled Full attempt
        # must not fall through into Read and open the camera anyway.
        self._raise_if_cancelled(f"before requesting {mode} access")
        try:
            cam.set_access_mode(access_enum)
        except vmbpy.VmbCameraError as e:
            self._raise_if_cancelled(f"after {mode} access was denied")
            raise _AccessDenialError(
                f"AccessMode.{mode.capitalize()} denied at "
                f"set_access_mode: {e}"
            ) from e
        self._raise_if_cancelled(f"after {mode} access was granted")

        txn = _HardwareTransaction(self._readback_matches)
        past_open = False
        try:
            with cam:
                past_open = True
                try:
                    # Opening the camera can itself block long enough for
                    # connect() to time out and disconnect() to run.
                    self._raise_if_cancelled("after opening the camera")
                    self._configure_exposure(cam, mode, txn)
                    # Full mode only — Read is passive and writes nothing. Each
                    # call snapshots the original and registers rollback before
                    # invoking the setter, and re-checks cancellation on both
                    # sides of it.
                    self._set_if_writable(cam, "TriggerSource", "Software", mode, txn)
                    self._set_if_writable(cam, "TriggerSelector", "FrameStart", mode, txn)
                    self._set_if_writable(cam, "TriggerMode", "On", mode, txn)
                    self._set_if_writable(cam, "AcquisitionMode", "Continuous", mode, txn)

                    # Read only, after every requested mutation has its final
                    # readback and before the callback thread can contend for
                    # the camera.  Publication is deferred to the transaction
                    # commit below: a start_streaming failure must not leave a
                    # snapshot that looks like a successful connection.
                    pending_sensor_settings = self._read_sensor_settings(
                        cam, mode,
                    )

                    # Never start acquisition for an abandoned cycle: it would
                    # hold the camera against kSA with no consumer.
                    self._raise_if_cancelled("immediately before start_streaming")
                    # Inside the transaction, and marked before invoking:
                    # start_streaming can partially succeed and then raise, so
                    # rollback must attempt stop_streaming regardless.
                    txn.streaming_attempted = True
                    cam.start_streaming(self._frame_handler)
                    try:
                        # TERMINAL DECISION — exactly one outcome wins.
                        #
                        # Between the last cancellation check and publishing
                        # "ready" there was a window where connect() could time
                        # out and report failure while this thread went on to
                        # commit. The lock makes the choice atomic: either the
                        # cycle was already cancelled (roll everything back) or
                        # this commit lands and later cancellation is a normal
                        # disconnect of a genuinely live camera.
                        with self._lifecycle_lock:
                            if self._stop_event.is_set():
                                raise self._CancelledError(
                                    "Vimba setup cancelled at the commit point; "
                                    "the connect cycle was abandoned."
                                )
                            txn.committed = True
                            self._active_access_mode = mode
                            with self._sensor_settings_lock:
                                self._sensor_settings_at_connect = deepcopy(
                                    pending_sensor_settings,
                                )
                            self._ready_event.set()
                        log.info(
                            "VmbCamera connected in AccessMode.%s "
                            "(camera_index=%d, trigger_hz=%.2f)",
                            mode.capitalize(), self._camera_index, self._trigger_hz,
                        )
                        self._trigger_and_idle_loop(cam, mode)
                    finally:
                        self._stop_streaming_or_flag_unknown(cam)
                        # Past a successful commit the exposure is deliberately
                        # left applied: it is volatile and documented to
                        # survive until power-cycle, and a normal DISARM must
                        # not silently revert what the grower saw confirmed.
                        txn.streaming_attempted = False
                finally:
                    # ROLLBACK RUNS HERE, still inside `with cam:`. Doing it in
                    # the outer finally meant every restoring set() went to a
                    # camera whose context had already exited — the writes
                    # went nowhere and the camera stayed modified while the
                    # driver reported a clean rollback.
                    if not txn.committed:
                        pending = sys.exc_info()[1] or RuntimeError(
                            "ARM did not complete"
                        )
                        txn.rollback(cam, pending)
        except vmbpy.VmbCameraError as e:
            if not past_open:
                raise _AccessDenialError(
                    f"AccessMode.{mode.capitalize()} denied at "
                    f"__enter__: {e}"
                ) from e
            raise

    # Acquisition settings that materially affect the image distribution or
    # identify the camera which produced it.  Firmware families expose
    # different names, so each logical field has an ordered candidate list;
    # the first readable candidate wins and its feature name is recorded too.
    _SENSOR_SETTING_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
        # ExposureTimeAbs/ExposureTime are specified in microseconds.  The raw
        # register is deliberately separate: its unit is camera/firmware
        # specific and must never be mislabeled as microseconds.
        ("exposure_us", ("ExposureTimeAbs", "ExposureTime")),
        ("exposure_raw", ("ExposureTimeRaw",)),
        ("exposure_auto", ("ExposureAuto",)),
        ("exposure_mode", ("ExposureMode",)),
        ("gain", ("Gain", "GainRaw")),
        ("gain_auto", ("GainAuto",)),
        ("black_level", ("BlackLevel", "BlackLevelRaw")),
        ("gamma", ("Gamma",)),
        ("pixel_format", ("PixelFormat",)),
        ("width", ("Width",)),
        ("height", ("Height",)),
        ("width_max", ("WidthMax",)),
        ("height_max", ("HeightMax",)),
        ("offset_x", ("OffsetX",)),
        ("offset_y", ("OffsetY",)),
        ("binning_horizontal", ("BinningHorizontal",)),
        ("binning_vertical", ("BinningVertical",)),
        ("binning_horizontal_mode", ("BinningHorizontalMode",)),
        ("binning_vertical_mode", ("BinningVerticalMode",)),
        ("decimation_horizontal", ("DecimationHorizontal",)),
        ("decimation_vertical", ("DecimationVertical",)),
        ("decimation_horizontal_mode", ("DecimationHorizontalMode",)),
        ("decimation_vertical_mode", ("DecimationVerticalMode",)),
        ("reverse_x", ("ReverseX",)),
        ("reverse_y", ("ReverseY",)),
        ("region_selector", ("RegionSelector",)),
        ("timestamp_tick_frequency_hz", ("GevTimestampTickFrequency",)),
        ("device_serial", ("DeviceSerialNumber",)),
        ("device_firmware", ("DeviceFirmwareVersion",)),
    )

    _GEOMETRY_REQUIRED = (
        "width", "height", "width_max", "height_max", "offset_x", "offset_y",
        "binning_horizontal", "binning_vertical", "decimation_horizontal",
        "decimation_vertical", "reverse_x", "reverse_y",
    )
    _GEOMETRY_OPTIONAL = (
        "binning_horizontal_mode", "binning_vertical_mode",
        "decimation_horizontal_mode", "decimation_vertical_mode",
        "region_selector",
    )

    @staticmethod
    def _geometry_scalar(key: str, value):
        """Canonicalize one spatial readback or reject an ambiguous value."""

        if key in {"reverse_x", "reverse_y"}:
            if isinstance(value, bool):
                return value
            raise ValueError(f"{key} must be boolean")
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer, not boolean")
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{key} must be a finite integer")
        result = int(number)
        if key in {"width", "height", "width_max", "height_max"} and result <= 0:
            raise ValueError(f"{key} must be positive")
        if key in {"offset_x", "offset_y"} and result < 0:
            raise ValueError(f"{key} must be non-negative")
        if key.startswith(("binning_", "decimation_")) and result < 1:
            raise ValueError(f"{key} must be at least one")
        return result

    @classmethod
    def _capture_geometry_metadata(
        cls, settings: dict, feature_status: dict,
    ) -> dict:
        """Derive a stable geometry identity only from proven readbacks.

        Every geometry-affecting value must be confirmed by the camera.  A
        missing node is not evidence of its neutral value, so missing and
        unreadable nodes both leave geometry unknown and the identifier empty.
        WidthMax/HeightMax are required so a cropped ROI is never mislabeled
        as full-frame based on dimensions alone.
        """

        fields: dict[str, object] = {}
        problems: list[str] = []
        for key in cls._GEOMETRY_REQUIRED:
            status = str(feature_status.get(key, {}).get("status") or "missing")
            if status == "confirmed":
                raw = settings.get(key)
            else:
                problems.append(f"{key}:{status}")
                continue
            try:
                fields[key] = cls._geometry_scalar(key, raw)
            except (TypeError, ValueError, OverflowError):
                problems.append(f"{key}:invalid")

        for key in cls._GEOMETRY_OPTIONAL:
            status = str(feature_status.get(key, {}).get("status") or "missing")
            if status == "confirmed":
                fields[key] = str(settings.get(key))
            elif status not in {"not_exposed"}:
                # These selectors/modes can change pixel correspondence. If a
                # node exists but is unreadable, do not mint a geometry ID.
                problems.append(f"{key}:{status}")

        complete = not problems
        geometry_id = ""
        full_frame = False
        capture_region = "unknown"
        if complete:
            canonical = {
                "schema": "vimba-capture-geometry-v1",
                "fields": fields,
            }
            digest = hashlib.sha256(
                json.dumps(
                    canonical, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True, allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            geometry_id = f"vimba-geometry-v1:sha256:{digest}"
            full_frame = bool(
                fields["offset_x"] == 0
                and fields["offset_y"] == 0
                and fields["width"] == fields["width_max"]
                and fields["height"] == fields["height_max"]
            )
            capture_region = "full_frame" if full_frame else "roi"
        return {
            "schema": "vimba-capture-geometry-v1",
            "readback_complete": complete,
            "capture_geometry_id": geometry_id,
            "capture_region": capture_region,
            "full_frame_confirmed": full_frame,
            "fields": fields,
            "problems": problems,
        }

    def _read_sensor_settings(self, cam, mode: str) -> dict:
        """Return a non-fatal, read-only snapshot of camera-open settings.

        This is deliberately a point-in-time record, not a claim that the
        values stayed constant for the session.  Another application can
        change camera features later, so ``read_at_utc`` is part of the
        contract.  Missing, unreadable, or oddly typed features are omitted;
        provenance collection must never make ARM fail.
        """
        settings: dict = {
            "read_at_utc": (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "access_mode": str(mode),
        }
        feature_status: dict[str, dict[str, str]] = {}
        for key, candidates in self._SENSOR_SETTING_CANDIDATES:
            encountered = False
            failures: list[str] = []
            for feature_name in candidates:
                try:
                    feature = getattr(cam, feature_name)
                except AttributeError:
                    continue
                except Exception as exc:  # noqa: BLE001 — SDK feature lookup
                    encountered = True
                    failures.append(f"{feature_name}:lookup:{type(exc).__name__}")
                    continue
                encountered = True
                try:
                    value = feature.get()
                    if not isinstance(value, (str, int, float, bool)):
                        value = str(value)
                except Exception as exc:  # noqa: BLE001 — optional SDK provenance
                    failures.append(f"{feature_name}:read:{type(exc).__name__}")
                    continue
                settings[key] = value
                settings[f"{key}_feature"] = feature_name
                feature_status[key] = {
                    "status": "confirmed", "feature": feature_name,
                }
                break
            else:
                feature_status[key] = {
                    "status": "unreadable" if encountered else "not_exposed",
                    "detail": ",".join(failures),
                }
        unavailable = [
            key for key, status in feature_status.items()
            if status["status"] != "confirmed"
        ]
        if unavailable:
            log.info(
                "VmbCamera: optional camera-open settings unavailable: %s",
                ", ".join(unavailable),
            )
        settings["feature_read_status"] = feature_status
        geometry = self._capture_geometry_metadata(settings, feature_status)
        settings["capture_geometry"] = geometry
        settings["capture_geometry_id"] = geometry["capture_geometry_id"]
        settings["geometry_readback_complete"] = geometry["readback_complete"]
        settings["full_frame_confirmed"] = geometry["full_frame_confirmed"]
        return settings

    def _stop_streaming_or_flag_unknown(self, cam) -> None:
        """Stop acquisition, or mark the camera unusable until power-cycled.

        A stream that may be running and cannot be stopped is not a tidy
        failure: the camera is still producing into a pipeline nobody owns,
        and a second ARM would attach to hardware in an unknown state. The
        flag survives this driver instance via the lease registry, so an
        immediate re-ARM is refused rather than silently reattaching.
        """
        try:
            cam.stop_streaming()
        except Exception as exc:  # noqa: BLE001
            self._unknown_hardware_state = (
                f"stop_streaming() failed ({exc}); acquisition may still be "
                "running on this camera"
            )
            log.critical(
                "VmbCamera could not stop streaming: %s. The camera is in an "
                "UNKNOWN state — POWER-CYCLE it before arming again.", exc,
            )

    def _readback_matches(self, feature, expected) -> bool:
        """Whether a restore provably landed on the original value."""
        actual = self._accessor_call(feature, "get")
        if actual is UNREADABLE or actual is ABSENT:
            return False
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if not self._finite_real(actual):
                return False
            return math.isclose(
                float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-6,
            )
        return actual == expected

    def _set_if_writable(self, cam, feature_name: str, value, mode: str,
                         txn=None) -> None:
        """Set a camera feature only if `feature.get_access_mode()[1]` is True.

        Discovery-driven — the driver asks the SDK, not the driver's own
        assumptions about what Read forbids. If `get_access_mode()` itself
        raises (undocumented SDK behavior):

        * Full mode: propagate the exception. Silently skipping required
          trigger config would leave the camera in an unusable state.
        * Read mode: log at INFO and skip. The driver is intentionally
          passive; a probe failure is bounded and non-fatal.
        """
        # Read mode is the kSA-coexistence path and is PASSIVE: it performs
        # zero configuration writes, regardless of what the writable bit says.
        # Gating on writability alone meant a camera reporting these features
        # as writable in Read mode got its trigger pipeline reconfigured out
        # from under kSA — the exact interference the mode exists to avoid.
        if mode != "full":
            log.info(
                "VmbCamera[Read]: not setting %s — Read mode performs no "
                "configuration writes.", feature_name,
            )
            return

        self._raise_if_cancelled(f"before writing {feature_name}")
        feature = getattr(cam, feature_name)
        try:
            access = feature.get_access_mode()
        except Exception:  # noqa: BLE001
            raise
        # An exact two-Boolean tuple. Unpacking and testing `not writable`
        # authorised the write for ("True", "False"), (1, 1) and any other
        # truthy shape — every non-empty value is truthy.
        if not (
            isinstance(access, tuple)
            and len(access) == 2
            and all(isinstance(bit, bool) for bit in access)
        ):
            raise RuntimeError(
                f"{feature_name}.get_access_mode() returned {access!r}; "
                "expected a (readable, writable) pair of booleans"
            )
        _readable, writable = access
        if writable is not True:
            log.info(
                "VmbCamera[Full]: skipping %s.set(%r) — reported read-only",
                feature_name, value,
            )
            return

        # A blocked get_access_mode() can span the whole timeout, so re-check
        # right before touching the camera.
        self._raise_if_cancelled(f"before setting {feature_name}")

        # Snapshot and REGISTER ROLLBACK BEFORE invoking: a setter can apply
        # the value and then block or raise, so registration afterwards would
        # miss exactly the mutation that needs undoing.
        original = self._accessor_call(feature, "get")
        if original is UNREADABLE or original is ABSENT:
            # No readable original means no possible rollback. Writing anyway
            # would leave a mutation the driver cannot undo, so the ARM is
            # refused instead — the camera stays as the grower left it.
            raise RuntimeError(
                f"{feature_name} could not report its current value, so the "
                "write could not be rolled back if ARM fails — refusing to "
                "modify a feature this driver cannot restore"
            )
        if txn is not None:
            txn.record(feature_name, feature, original)
        feature.set(value)
        # And immediately after: a blocked setter that applies on release must
        # still reach rollback rather than continuing into start_streaming.
        self._raise_if_cancelled(f"immediately after setting {feature_name}")

    def _trigger_and_idle_loop(self, cam, mode: str) -> None:
        """Per-mode trigger/idle loop; exits when `_stop_event` is set.

        * Full mode: software-triggers at trigger_hz. Bounded consecutive-
          fail counter forces a short backoff so a transient (network
          hiccup, kSA cycling the port) has room to recover without
          spinning the CPU. If TriggerSoftware.run() raises unexpectedly
          the real error is recorded through `_last_frame_error` — we
          do NOT silently degrade to a passive loop.
        * Read mode: pure idle wait. Frames arrive from whoever holds
          Full (kSA, typically) plus Vimba multicast; the driver never
          triggers. No `TriggerSoftware.run()` call at all.
        """
        period = 1.0 / self._trigger_hz
        consecutive_trigger_fails = 0
        while not self._stop_event.is_set():
            # Serviced BEFORE the trigger, so the very next integration is the
            # first one taken at the new exposure. Doing it after would emit
            # one frame at the old value that the GUI has already been told is
            # the new one.
            self._service_exposure_request(cam, mode)
            if mode == "full":
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
            # Interruptible pacing — disconnect() wakes this at once
            # instead of after a full period. In Read mode this is the
            # only thing the loop does; frames flow via the callback.
            self._stop_event.wait(period)

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
                self._latest_frame_sequence += 1
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
            if self._latest_frame is not None:
                if self._latest_frame_sequence <= self._last_delivered_sequence:
                    raise FrameNotYetAvailableError(
                        "No new Vimba frame has arrived since the previous "
                        "read; the cached image was not re-served as a new "
                        "acquisition."
                    )
                # Copy and mark consumed under the same lock as the callback's
                # update so a new arrival cannot race sequence bookkeeping.
                result = self._latest_frame.copy()
                self._last_delivered_sequence = self._latest_frame_sequence
                return result

        # No frame has arrived in this connect cycle.
        if self._active_access_mode == "read":
            raise FrameNotYetAvailableError(
                "Connected in AccessMode.Read; no frame arrived yet. "
                "Frames flow only if another consumer (e.g. kSA Live "
                "Video) is triggering, camera multicast is enabled "
                "(Vimba X Viewer → user set), and external triggering "
                "is producing frames."
            )
        raise FrameNotYetAvailableError(
            "Vimba camera is streaming but no frame has arrived yet — "
            "expected within one trigger period after connect."
        )

    def request_stop(self) -> None:
        """Signal cancellation without blocking. Safe from any thread.

        RheedCameraWorker.stop() previously only cleared its own `running`
        flag. A worker blocked inside connect() never reached the check, so
        the driver's stop event stayed unset and setup carried on writing
        exposure and starting streams after the grower pressed DISARM.

        Deliberately does NOT join or touch the SDK: cleanup belongs to the
        thread that owns the camera, and blocking here would just move the
        stall into the GUI thread.

        Takes the lifecycle lock so cancellation and commit cannot interleave:
        without it a stop arriving mid-commit could be observed as "not
        cancelled" by the setup thread and "cancelled" by the caller.
        """
        with self._lifecycle_lock:
            self._stop_event.set()

    def disconnect(self) -> None:
        with self._lifecycle_lock:
            self._connected = False
            self._stop_event.set()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=self.DISCONNECT_TIMEOUT_S)
            if self._stream_thread.is_alive():
                # RETAIN the reference. Clearing it here made the orphan
                # invisible, so the next connect() started a second thread
                # against the same camera while this one was still blocked
                # inside an SDK call — and it could still perform its exposure
                # write after the GUI had reported ARM failed and returned to
                # idle. connect() now refuses while this is alive, and only a
                # thread that has actually died clears the slot.
                log.warning(
                    "VmbCamera stream thread did not exit within %.1fs; "
                    "retaining the reference so a reconnect cannot race it.",
                    self.DISCONNECT_TIMEOUT_S,
                )
            else:
                self._stream_thread = None
        with self._frame_lock:
            self._latest_frame = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def access_mode(self) -> str:
        """The negotiated AccessMode after a successful open.

        Returns "full" or "read" while streaming, "" before connect or
        after disconnect. Useful for diagnostics and for the precheck
        script (`scripts/precheck_direct_camera.py`) to report which
        mode was actually granted when the driver was constructed with
        `access_mode="auto"`.
        """
        return self._active_access_mode

    @property
    def exposure_us(self) -> Optional[float]:
        """Confirmed camera exposure readback for the active connect cycle."""
        return self._exposure_us

    @property
    def exposure_generation(self) -> int:
        """Count of confirmed live exposure changes on this connect cycle."""
        with self._exposure_request_lock:
            return self._exposure_generation

    @property
    def last_exposure_error(self) -> str:
        """Why the most recent live exposure request was refused, or ""."""
        with self._exposure_request_lock:
            return self._last_exposure_error

    def request_exposure_us(self, value: float) -> None:
        """Ask the stream thread to apply a new exposure without re-arming.

        Returns as soon as the request is parked; the write happens on the
        stream thread and its outcome surfaces through ``exposure_us``,
        ``exposure_generation`` and ``last_exposure_error``.

        Everything checkable without touching the camera is checked HERE, so a
        bad value is refused while the caller is still on the stack and can be
        told why. The device-side gates (range, increment grid, ExposureAuto,
        readback) belong to ``_apply_exposure_write`` and run on the stream
        thread, because only it may talk to the SDK.
        """
        if not self._usable_number(value):
            raise ValueError(
                f"exposure_us must be a finite positive number, got {value!r}"
            )
        requested = float(value)
        safe_max_us = (
            1_000_000.0 / self._trigger_hz * self._MAX_EXPOSURE_PERIOD_FRACTION
        )
        if requested > safe_max_us:
            raise ValueError(
                f"exposure_us={requested:.0f} is too long for "
                f"trigger_hz={self._trigger_hz:.3g}; use <= {safe_max_us:.0f} "
                "us to preserve 10% acquisition headroom"
            )
        if not self._connected:
            raise RuntimeError(
                "Cannot change exposure: the camera is not connected. ARM "
                "first, then adjust the exposure live."
            )
        # Read mode means another process (kSA, typically) holds the exclusive
        # lock and owns acquisition. Writing exposure from a passive consumer
        # would either fail at the SDK or silently change what the grower sees
        # in kSA. Refuse with the cause named, rather than parking a request
        # the stream thread can only reject later out of context.
        if self._active_access_mode != "full":
            raise RuntimeError(
                "Cannot change exposure in Read access mode — kSA or the "
                "Vimba X Viewer holds the camera. Close it, then disarm and "
                "arm again to negotiate Full access."
            )
        with self._exposure_request_lock:
            self._pending_exposure_us = requested
            self._last_exposure_error = ""

    def _service_exposure_request(self, cam, mode: str) -> None:
        """Apply one pending live exposure change. Runs on the stream thread.

        Failure is recorded, never raised: a refused exposure must not tear
        down a healthy acquisition. ``_apply_exposure_write`` restores the
        previous value before it raises, so the camera is left where the
        grower last confirmed it.
        """
        with self._exposure_request_lock:
            requested = self._pending_exposure_us
            self._pending_exposure_us = None
        if requested is None:
            return

        try:
            feature_name = next(
                (
                    name for name in self._EXPOSURE_FEATURE_CANDIDATES
                    if self._optional_feature(cam, name) is not None
                ),
                None,
            )
            if feature_name is None:
                raise RuntimeError(
                    "Live exposure requested, but neither ExposureTimeAbs nor "
                    "ExposureTime exists on this camera"
                )
            feature = self._optional_feature(cam, feature_name)
            current = self._accessor_call(feature, "get")
            # The restore target must be a usable number BEFORE the write, for
            # the same reason as at ARM: a value we could not hand back makes
            # the write a one-way change to the grower's camera.
            if not self._usable_number(current):
                raise RuntimeError(
                    f"Could not read a usable current {feature_name} "
                    f"({current!r}); refusing a live write that could not "
                    "then be undone"
                )
            confirmed = self._apply_exposure_write(
                cam, mode, feature_name, feature, float(current), requested,
            )
        except Exception as exc:  # noqa: BLE001 — a refusal is not a fault
            with self._exposure_request_lock:
                self._last_exposure_error = str(exc)
            log.error("Live exposure change refused: %s", exc)
            return

        with self._exposure_request_lock:
            self._exposure_generation += 1
            self._last_exposure_error = ""
            generation = self._exposure_generation
        log.info(
            "VmbCamera live exposure applied: %.0f us confirmed "
            "(requested %.0f us, generation %d, volatile)",
            confirmed, requested, generation,
        )

    @property
    def sensor_settings_at_connect(self) -> dict:
        """Defensive copy of the most recent successful camera-open snapshot.

        The record survives disconnect so STOP and window-close can persist it
        after worker teardown.  It is empty before a successful connect and
        is explicitly timestamped because external camera software may change
        settings later in the session.
        """
        with self._sensor_settings_lock:
            return deepcopy(self._sensor_settings_at_connect)

    @property
    def capture_geometry_id(self) -> str:
        """Spatial identity from the successful connect-cycle readback.

        Empty means the required geometry could not be proven.  In
        particular this never falls back to a dimensions-only ``full-frame``
        claim.
        """
        with self._sensor_settings_lock:
            return str(
                self._sensor_settings_at_connect.get("capture_geometry_id")
                or ""
            )


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
        dpi_provider: Optional[Callable[[int], int]] = None,
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
        self._last_crop_status = "disabled" if not crop_chrome else "unobserved"
        self._first_frame_timeout_s = first_frame_timeout_s
        self._stale_timeout_s = stale_timeout_s
        self._capture_factory = capture_factory
        self._dpi_provider = dpi_provider
        self._wgc_dpi_identity = f"dpi-fallback-{_DEFAULT_WINDOW_DPI}"
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
            cropped = self._crop_chrome_pixels(first.image)
            try:
                self._require_effective_crop()
            except RuntimeError:
                session.close()
                self._capture_session = None
                raise
            self._last_capture = first.with_image(cropped)
            self._refresh_wgc_dpi_identity(hwnd)
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
                self._require_effective_crop()
                self._last_capture = sample.with_image(cropped)
                self._refresh_wgc_dpi_identity(sample.source_hwnd)
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
            self._last_crop_status = "disabled"
            return frame
        h = frame.shape[0]
        top = max(0, self._chrome_top_px)
        bottom = h - max(0, self._chrome_bottom_px)
        if bottom <= top:
            self._last_crop_status = "fallback-full"
            return frame
        self._last_crop_status = "applied"
        return frame[top:bottom, :]

    def _require_effective_crop(self) -> None:
        """Reject WGC frames when configured chrome removal could not run."""
        if self._crop_chrome and self._last_crop_status != "applied":
            raise RuntimeError(
                "kSA chrome crop is invalid for the captured window size; "
                "restore/re-size Live Video and recalibrate"
            )

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

    @property
    def capture_geometry_id(self) -> str:
        """Stable identity for the crop policy applied to returned frames."""
        crop_identity = (
            f"ksa-chrome-v2:{int(self._crop_chrome)}:"
            f"{int(self._chrome_top_px)}:{int(self._chrome_bottom_px)}:"
            f"{self._last_crop_status}"
        )
        # Keep the explicit legacy mss identifier byte-for-byte compatible.
        # WGC needs DPI in its identity because the fixed kSA chrome crop is
        # measured in physical pixels and can shift across DPI contexts.
        if self._capture_method != "wgc":
            return crop_identity
        return f"{crop_identity}:{self._wgc_dpi_identity}"

    def _refresh_wgc_dpi_identity(self, hwnd: int) -> None:
        """Refresh the DPI component paired with the next returned frame."""
        self._wgc_dpi_identity = _window_dpi_identity(hwnd, self._dpi_provider)

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
        self._last_crop_status = (
            "disabled" if not self._crop_chrome else "unobserved"
        )

    @property
    def connected(self) -> bool:
        return self._connected


class DummyCamera(RheedCamera):
    """Stable experimental STO frames for manual Equalizer development.

    The image is shown without an orientation transform: the camera pane stays
    the reference and calibration warps the simulator basis to match it.
    """

    PRESETS: dict[str, str] = {
        "dummy": "1x1",
        "dummy_c6x2": "c6x2",
        "dummy_tw": "Twinned2x1",
        "dummy_rt13_tilted": "RT13",
    }
    SOURCE_FILENAMES: dict[str, str] = {
        "dummy": "1x1_1.bmp",
        "dummy_c6x2": "c6x2_1.bmp",
        "dummy_tw": "Twinned2x1_1.bmp",
        "dummy_rt13_tilted": "RT13_20.png",
    }
    ROTATION_DEGREES: dict[str, float] = {
        "dummy_rt13_tilted": 15.0,
    }
    DEFAULT_PRESET = "dummy"

    def __init__(
        self,
        width: int = 656,
        height: int = 492,
        preset: Optional[str] = None,
        data_root: Optional[Path] = None,
    ):
        self._width = width
        self._height = height
        self._connected = False
        self._frame_count = 0
        self._preset = (
            preset if preset in self.PRESETS else self.DEFAULT_PRESET
        )
        self._data_root = (
            Path(data_root)
            if data_root is not None
            else Path(__file__).resolve().parents[1] / "data" / "dummy_camera"
        )
        self._source_image: Optional[np.ndarray] = None
        self._source_path: Optional[Path] = None

    @property
    def preset(self) -> str:
        return self._preset

    @property
    def source_path(self) -> Optional[Path]:
        return self._source_path

    def _load_image(self) -> None:
        if self._source_image is not None:
            return
        source_path = self._data_root / self.SOURCE_FILENAMES[self._preset]
        if not source_path.is_file():
            raise FileNotFoundError(
                f"DummyCamera asset missing for {self._preset}: {source_path}"
            )
        try:
            from PIL import Image

            with Image.open(source_path) as image:
                self._source_image = np.asarray(
                    image.convert("L"), dtype=np.uint8,
                ).copy()
            self._source_path = source_path
        except (ImportError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"Could not load DummyCamera preset {self._preset} "
                f"from {source_path}: {exc}"
            ) from exc

    def connect(self) -> None:
        self._connected = False
        self._frame_count = 0
        self._load_image()
        self._connected = True

    def read_frame(self) -> np.ndarray:
        if not self._connected:
            raise RuntimeError("Dummy camera not connected.")
        self._load_image()
        frame = np.zeros((self._height, self._width, 3), dtype=np.uint8)

        if self._source_image is None:
            raise RuntimeError("DummyCamera has no loaded source image.")
        from PIL import Image

        rendered = Image.fromarray(self._source_image).resize(
            (self._width, self._height),
            Image.Resampling.BILINEAR,
        )
        rotation_degrees = self.ROTATION_DEGREES.get(self._preset, 0.0)
        if rotation_degrees:
            rendered = rendered.rotate(
                rotation_degrees,
                resample=Image.Resampling.BICUBIC,
                expand=False,
                fillcolor=0,
            )
        display = np.asarray(rendered, dtype=np.uint8)

        frame[:, :, 1] = display
        frame[:, :, 2] = (display // 3).astype(np.uint8)

        self._frame_count += 1
        return frame

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected
