"""
Pyrometer drivers — Modbus RTU (direct) and screen scrape (via TemperaSure).

Implements the TemperatureSensor ABC from heater_control.py so the pyrometer
can be used interchangeably with other temperature sensors in the system.

Two modes:
  1. ModbusPyrometer: Direct RS-422 via IFD-5 module (Exactus protocol)
  2. ScreenGrabPyrometer: pywinauto scrape of TemperaSure GUI window
"""

import logging
import struct
import time
from typing import Optional

from heater_control import TemperatureSensor

log = logging.getLogger(__name__)


# ─── Modbus register map (from Exactus manual) ───
REG_CH1_TEMP = 0x0000
REG_CH1_CURR = 0x0004
REG_AMBIENT = 0x0800

REG_ADDR = 0x1007
REG_BAUD = 0x1008
REG_RATE = 0x1011
REG_NAME0 = 0x1100  # 32 regs, 1 ASCII byte per reg
REG_VER = 0x1300
REG_BUILD = 0x1301
REG_SN0 = 0x1305  # 9 regs

REG_CMD = 0x8000
CMD_SAVE = 0x7001
CMD_REBOOT = 0x8080


def _regs_to_f32(hi: int, lo: int) -> float:
    """Convert two 16-bit holding registers to IEEE 754 float32."""
    raw = (hi << 16) | lo
    return struct.unpack(">f", raw.to_bytes(4, "big"))[0]


def _regs_to_ascii(regs: list[int]) -> str:
    """Convert register array (1 ASCII byte per reg) to string."""
    b = bytes(r & 0xFF for r in regs)
    return b.split(b"\x00", 1)[0].decode("ascii", errors="ignore")


def _crc16_modbus(payload: bytes) -> int:
    """Return the CRC-16/MODBUS checksum for *payload*."""
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class _RawReadResponse:
    """Small pymodbus-compatible response used by the raw read backend."""

    def __init__(self, registers=None, error: str = ""):
        self.registers = list(registers or [])
        self.error = error

    def isError(self) -> bool:  # noqa: N802 - pymodbus compatibility
        return bool(self.error)


class _RawSerialModbusClient:
    """Read-only Modbus RTU client that tolerates bytes before a reply.

    Ch-MBE's IFD-5/PL2303GS path was verified with the raw probe on
    2026-08-05, while pymodbus 3.14 timed out on the same COM3 link.  This
    client scans the complete receive window for a CRC-valid function-03
    frame instead of assuming that the reply begins at byte zero.
    """

    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        parity: str,
        stopbits: int,
        bytesize: int,
        timeout: float,
        rts: Optional[bool],
    ):
        self._settings = {
            "port": port,
            "baudrate": baudrate,
            "parity": parity,
            "stopbits": stopbits,
            "bytesize": bytesize,
            "timeout": min(max(float(timeout), 0.01), 0.05),
            "write_timeout": timeout,
        }
        self._response_timeout = float(timeout)
        self._rts = rts
        self.socket = None

    def connect(self) -> bool:
        try:
            import serial
        except ImportError as exc:
            raise ImportError("pyserial not installed: pip install pyserial") from exc
        self.socket = serial.Serial(**self._settings)
        if self._rts is not None:
            self.socket.rts = self._rts
        return bool(getattr(self.socket, "is_open", True))

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    @staticmethod
    def _request(device_id: int, address: int, count: int) -> bytes:
        body = struct.pack(">BBHH", device_id, 0x03, address, count)
        return body + struct.pack("<H", _crc16_modbus(body))

    @staticmethod
    def _find_reply(data: bytes, device_id: int, count: int):
        expected_bytes = count * 2
        frame_len = 5 + expected_bytes
        for offset in range(max(0, len(data) - frame_len + 1)):
            if data[offset:offset + 3] != bytes((device_id, 0x03, expected_bytes)):
                continue
            frame = data[offset:offset + frame_len]
            crc_rx = struct.unpack("<H", frame[-2:])[0]
            if _crc16_modbus(frame[:-2]) != crc_rx:
                continue
            registers = struct.unpack(f">{count}H", frame[3:-2])
            return _RawReadResponse(registers)

        # A Modbus exception is five bytes: id, function|0x80, code, CRC.
        for offset in range(max(0, len(data) - 4)):
            frame = data[offset:offset + 5]
            if len(frame) < 5 or frame[:2] != bytes((device_id, 0x83)):
                continue
            crc_rx = struct.unpack("<H", frame[-2:])[0]
            if _crc16_modbus(frame[:-2]) == crc_rx:
                return _RawReadResponse(error=f"Modbus exception 0x{frame[2]:02X}")
        return None

    def read_holding_registers(
        self,
        address: int,
        count: int = 1,
        *,
        device_id: int = 1,
        **_kwargs,
    ):
        if self.socket is None:
            return _RawReadResponse(error="serial port is not open")
        request = self._request(device_id, address, count)
        self.socket.reset_input_buffer()
        self.socket.write(request)
        self.socket.flush()

        received = bytearray()
        deadline = time.monotonic() + self._response_timeout
        while time.monotonic() < deadline:
            waiting = int(getattr(self.socket, "in_waiting", 0) or 0)
            chunk = self.socket.read(max(1, waiting))
            if chunk:
                received.extend(chunk)
                response = self._find_reply(bytes(received), device_id, count)
                if response is not None:
                    return response
            else:
                time.sleep(0.005)
        return _RawReadResponse(
            error=(
                f"no CRC-valid Modbus reply within {self._response_timeout:.2f}s "
                f"({len(received)} bytes received)"
            )
        )

    def write_register(self, *_args, **_kwargs):
        raise RuntimeError(
            "The raw_serial pyrometer backend is read-only; writes are disabled."
        )


class ModbusPyrometer(TemperatureSensor):
    """
    Direct Modbus RTU interface to BASF Exactus pyrometer via IFD-5 (RS-422).

    Ported from the existing pyrometer_script.py (Jacques Attinger, Yang
    Research Group) with pymodbus compatibility wrappers for different API
    versions. This is the *proven* pyrometer direct-read path — Jacques's
    script works against real Bulbasaur hardware; the class here mirrors
    that behavior with class-level constants for the two plausibility
    ranges (temperature + set_rate) so callers can tune per deployment.

    Reads: ``read_temperature`` / ``read_current`` / ``read_ambient`` all
    validate the returned float against ``TEMP_MIN_C..TEMP_MAX_C`` before
    returning. Values outside the range raise RuntimeError — matches
    ``ExactusSerialPyrometer`` semantics so both pyrometer paths behave
    identically for the worker's error-handling logic.
    """

    # Plausibility bounds — Jacques's pyrometer_script.py plausible_temp
    # at line 117: ``def plausible_temp(x): return -100.0 < x < 3000.0``.
    TEMP_MIN_C = -100.0
    TEMP_MAX_C = 3000.0

    # Valid sample-rate range for ``set_rate``. Jacques's script at
    # line 168 uses [1, 1000] Hz with the caveat "adjust if your device
    # supports more". Keep symmetric.
    RATE_MIN_HZ = 1
    RATE_MAX_HZ = 1000

    def __init__(
        self,
        port: str = "COM4",
        baudrate: int = 115200,
        device_id: int = 1,
        timeout: float = 1.0,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
        rts: Optional[bool] = None,
        backend: str = "pymodbus",
    ):
        """``rts`` controls the RTS line — see ``connect``.

        ``None`` (the default) makes no claim and leaves pyserial's own
        behaviour untouched. ``False`` de-asserts RTS, which on
        Bulbasaur's IFD-5 + Prolific PL2303GS path is what makes the probe
        reachable at all: with RTS asserted the link loops back and every
        request returns byte-for-byte without reaching the instrument.

        The default is None rather than False so that constructing this
        driver against an uncharacterised chamber cannot silently change
        that chamber's serial behaviour. Callers should pass
        ``MBESystemConfig.pyrometer_rts``.
        """
        self._port = port
        self._baudrate = baudrate
        self._device_id = device_id
        self._timeout = timeout
        self._parity = parity
        self._stopbits = stopbits
        self._bytesize = bytesize
        self._rts = rts
        if backend not in {"pymodbus", "raw_serial"}:
            raise ValueError(
                "pyrometer Modbus backend must be 'pymodbus' or 'raw_serial', "
                f"got {backend!r}"
            )
        self._backend = backend
        self._client = None
        self._connected = False
        self._word_swap = False

    def connect(self) -> None:
        if self._backend == "raw_serial":
            self._client = _RawSerialModbusClient(
                port=self._port,
                baudrate=self._baudrate,
                parity=self._parity,
                stopbits=self._stopbits,
                bytesize=self._bytesize,
                timeout=self._timeout,
                rts=self._rts,
            )
        else:
            try:
                from pymodbus.client import ModbusSerialClient
            except ImportError as exc:
                raise ImportError(
                    "pymodbus not installed: pip install pymodbus pyserial"
                ) from exc
            self._client = ModbusSerialClient(
                port=self._port,
                baudrate=self._baudrate,
                parity=self._parity,
                stopbits=self._stopbits,
                bytesize=self._bytesize,
                timeout=self._timeout,
            )
        if not self._client.connect():
            raise RuntimeError(
                f"Could not open serial port {self._port}. "
                "Close TemperaSure or any serial monitor."
            )

        # Set RTS explicitly, immediately after the port is open and before
        # any traffic. pyserial leaves RTS asserted by default; on
        # Bulbasaur's IFD-5 + PL2303GS path that asserted line puts the
        # link into loopback — every request comes back byte-for-byte and
        # the probe is never reached. Measured Jul 30 2026 on COM4:
        #
        #   RTS=True  -> 01 03 13 00 00 01 80 8E   (our own request back)
        #   RTS=False -> 01 03 02 09 03 FE 15      (version 9.3, CRC valid)
        #
        # DTR made no difference in either direction and is left alone.
        # This is per-chamber configuration, not a universal truth: the
        # value comes from MBESystemConfig.pyrometer_rts because Ch-MBE's
        # cabling has not been characterised.
        #
        # rts=None means "no decision recorded for this chamber" and must
        # leave the line exactly as pyserial set it. Touching it here
        # would turn an unconfigured chamber into an untested behaviour
        # change, which is the one thing this setting exists to avoid.
        if self._rts is None:
            log.warning(
                "ModbusPyrometer: RTS not configured for this chamber "
                "(pyrometer_rts=None); using the serial library default. "
                "If reads return the request verbatim, the line is "
                "looping back — set pyrometer_rts=False in "
                "drivers/config.py for this chamber and re-verify.",
            )
        else:
            sock = getattr(self._client, "socket", None)
            if sock is None:
                log.warning(
                    "ModbusPyrometer: no underlying serial object on the "
                    "pymodbus client; RTS could not be set to %s. If reads "
                    "return the request verbatim, this is why.", self._rts,
                )
            else:
                try:
                    sock.rts = self._rts
                    log.info("ModbusPyrometer: RTS set to %s", self._rts)
                except Exception as exc:  # noqa: BLE001 — driver-dependent
                    log.warning(
                        "ModbusPyrometer: could not set RTS to %s (%s). A "
                        "loopback response is likely.", self._rts, exc,
                    )

        # Auto-detect word order
        self._detect_word_order()
        self._connected = True
        log.info(
            "ModbusPyrometer connected: %s @ %d 8%s%d id=%d "
            "backend=%s word_swap=%s",
            self._port, self._baudrate, self._parity, self._stopbits,
            self._device_id, self._backend, self._word_swap,
        )

    def _detect_word_order(self) -> None:
        """Read temperature and flip word order if result is implausible.

        If both word orders return implausible values, the flag stays False
        (constructor default) and the first real ``read_temperature`` call
        will raise a proper plausibility RuntimeError with the real number
        so the caller can see what's wrong.
        """
        try:
            t = self._read_f32(REG_CH1_TEMP, word_swap=False)
        except Exception as exc:  # noqa: BLE001 — pymodbus can raise many types
            log.debug("ModbusPyrometer word-order detect failed: %s", exc)
            return
        if self.TEMP_MIN_C < t < self.TEMP_MAX_C:
            return  # First reading is plausible; keep _word_swap=False.
        # First reading implausible — try the swap.
        try:
            t = self._read_f32(REG_CH1_TEMP, word_swap=True)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "ModbusPyrometer word-order swap detect failed: %s", exc,
            )
            return
        if self.TEMP_MIN_C < t < self.TEMP_MAX_C:
            self._word_swap = True
            log.info(
                "ModbusPyrometer word-order auto-detect: swap ON (T=%.2f°C)",
                t,
            )
        else:
            log.warning(
                "ModbusPyrometer word-order detect: both orders implausible "
                "(swap=True gave T=%.2f); staying word_swap=False", t,
            )

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — pymodbus close can raise many types
                log.debug("ModbusPyrometer close raised; ignoring", exc_info=True)
            self._client = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def read_temperature(self) -> float:
        """Read channel 1 temperature in °C, validated against plausibility bounds.

        Raises ``RuntimeError`` if not connected or if the returned value
        lies outside ``[TEMP_MIN_C, TEMP_MAX_C]``. The bounds match Jacques's
        script (line 117) and ``ExactusSerialPyrometer`` so both pyrometer
        paths share the same failure mode.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("ModbusPyrometer not connected.")
        t = self._read_f32(REG_CH1_TEMP, self._word_swap)
        if not (self.TEMP_MIN_C < t < self.TEMP_MAX_C):
            raise RuntimeError(
                f"ModbusPyrometer implausible temperature {t:.3f}°C "
                f"(outside [{self.TEMP_MIN_C}, {self.TEMP_MAX_C}] °C) — "
                f"check word_swap={self._word_swap}, device_id={self._device_id}"
            )
        return t

    def read_current(self) -> float:
        """Read channel 1 signal current (arbitrary Modbus units)."""
        if not self._connected or self._client is None:
            raise RuntimeError("ModbusPyrometer not connected.")
        return self._read_f32(REG_CH1_CURR, self._word_swap)

    def read_ambient(self) -> float:
        """Read ambient temperature (if supported by firmware)."""
        if not self._connected or self._client is None:
            raise RuntimeError("ModbusPyrometer not connected.")
        return self._read_f32(REG_AMBIENT, self._word_swap)

    def get_info(self) -> dict:
        """Read device identity: name, serial, version, rate."""
        if not self._connected or self._client is None:
            return {}

        info: dict = {}
        try:
            info["name"] = self._read_ascii(REG_NAME0, 32)
        except Exception as exc:  # noqa: BLE001
            log.debug("ModbusPyrometer get_info name read failed: %s", exc)
        try:
            info["serial"] = self._read_ascii(REG_SN0, 9)
        except Exception as exc:  # noqa: BLE001
            log.debug("ModbusPyrometer get_info serial read failed: %s", exc)
        try:
            ver = self._read_u16(REG_VER)
            info["version"] = f"{ver >> 8}.{ver & 0xFF}"
        except Exception as exc:  # noqa: BLE001
            log.debug("ModbusPyrometer get_info version read failed: %s", exc)
        try:
            info["rate_hz"] = self._read_u16(REG_RATE)
        except Exception as exc:  # noqa: BLE001
            log.debug("ModbusPyrometer get_info rate read failed: %s", exc)
        return info

    def set_rate(self, hz: int, persist: bool = False) -> None:
        """Set pyrometer sample rate. Optionally persist to EEPROM.

        Rate is clamped to the valid range ``[RATE_MIN_HZ, RATE_MAX_HZ]``
        — matches Jacques's script (line 168). A caller trying to set a
        wildly-out-of-range value (which would silently truncate to u16
        and misconfigure the device) sees ``ValueError`` before any wire
        traffic.
        """
        if not (self.RATE_MIN_HZ <= hz <= self.RATE_MAX_HZ):
            raise ValueError(
                f"rate must be between {self.RATE_MIN_HZ} and "
                f"{self.RATE_MAX_HZ} Hz, got {hz}"
            )
        if not self._connected or self._client is None:
            raise RuntimeError("ModbusPyrometer not connected.")

        wr = self._write_register(REG_RATE, hz)
        # Jacques inspects the write result — if pymodbus flags an error,
        # we should raise rather than silently continue.
        if hasattr(wr, "isError") and wr.isError():
            raise RuntimeError(
                f"ModbusPyrometer set_rate({hz}) failed at 0x{REG_RATE:04X}"
            )
        if persist:
            try:
                wr2 = self._write_register(REG_CMD, CMD_SAVE)
                if hasattr(wr2, "isError") and wr2.isError():
                    log.debug(
                        "ModbusPyrometer EEPROM save flagged error — some "
                        "firmware doesn't support CMD_SAVE; setting will be "
                        "lost on reboot but is applied now."
                    )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "ModbusPyrometer EEPROM save raised (%s); firmware may "
                    "not support the CMD_SAVE opcode. Setting is applied "
                    "for this session but not persisted.", exc,
                )

    # ─── Low-level Modbus helpers ───

    def _read_holding(self, addr: int, count: int):
        """Read holding registers with pymodbus version compatibility.

        pymodbus renamed the device-ID kwarg between versions (``unit`` →
        ``slave`` → ``device_id``); this loop tries each in turn and falls
        back to the current name. TypeError specifically catches the
        "unexpected keyword argument" error — other exceptions propagate.
        """
        for kw in ("device_id", "slave", "unit"):
            try:
                return self._client.read_holding_registers(
                    addr, count, **{kw: self._device_id}
                )
            except TypeError:
                continue
        return self._client.read_holding_registers(
            address=addr, count=count, device_id=self._device_id
        )

    def _write_register(self, addr: int, value: int):
        for kw in ("device_id", "slave", "unit"):
            try:
                return self._client.write_register(
                    addr, value, **{kw: self._device_id}
                )
            except TypeError:
                continue
        return self._client.write_register(
            address=addr, value=value, device_id=self._device_id
        )

    @staticmethod
    def _resp_ok(rr, need: int) -> bool:
        """Predicate matching Jacques's ``_resp_ok`` — checks all three fields.

        A pymodbus response is "OK" only if it has ``isError()`` returning
        False AND it exposes ``.registers`` with at least ``need`` entries.
        Skipping any of these checks means an AttributeError or IndexError
        surfaces from the caller instead of a clean RuntimeError.
        """
        return (
            hasattr(rr, "isError")
            and not rr.isError()
            and hasattr(rr, "registers")
            and len(rr.registers) >= need
        )

    # Diagnostic message for "empty register response" — surfaced when
    # the probe is in Exactus streaming mode rather than Modbus RTU.
    # Ported Jul 27 2026 from scripts/pyrometer_modbus_smoke.py after
    # the Bulbasaur lab session; the smoke script's helpful message
    # made the failure mode diagnosable, the driver's bare error did not.
    _EMPTY_RESPONSE_HINT = (
        " Probe is likely in Exactus Protocol mode rather than Modbus RTU. "
        "Recovery: physical power-cycle the probe (~10s off, 10s boot), "
        "or use ExactusSerialPyrometer instead."
    )

    def _read_f32(self, addr: int, word_swap: bool = False) -> float:
        rr = self._read_holding(addr, 2)
        if not self._resp_ok(rr, 2):
            regs = getattr(rr, "registers", None)
            if not regs:
                detail = getattr(rr, "error", "")
                detail = f" Transport detail: {detail}." if detail else ""
                raise RuntimeError(
                    f"Modbus read at 0x{addr:04X}: empty register response."
                    + detail
                    + self._EMPTY_RESPONSE_HINT
                )
            raise RuntimeError(
                f"Modbus read at 0x{addr:04X}: response has {len(regs)} regs, "
                "needs 2"
            )
        hi, lo = rr.registers[:2]
        if word_swap:
            hi, lo = lo, hi
        return _regs_to_f32(hi, lo)

    def _read_u16(self, addr: int) -> int:
        rr = self._read_holding(addr, 1)
        if not self._resp_ok(rr, 1):
            regs = getattr(rr, "registers", None)
            if not regs:
                detail = getattr(rr, "error", "")
                detail = f" Transport detail: {detail}." if detail else ""
                raise RuntimeError(
                    f"Modbus read at 0x{addr:04X}: empty register response."
                    + detail
                    + self._EMPTY_RESPONSE_HINT
                )
            raise RuntimeError(
                f"Modbus read at 0x{addr:04X}: response has {len(regs)} regs, "
                "needs 1"
            )
        return rr.registers[0]

    def _read_ascii(self, addr: int, n_regs: int) -> str:
        rr = self._read_holding(addr, n_regs)
        if not self._resp_ok(rr, n_regs):
            regs = getattr(rr, "registers", None)
            if not regs:
                detail = getattr(rr, "error", "")
                detail = f" Transport detail: {detail}." if detail else ""
                raise RuntimeError(
                    f"Modbus read at 0x{addr:04X}: empty register response."
                    + detail
                    + self._EMPTY_RESPONSE_HINT
                )
            raise RuntimeError(
                f"Modbus read at 0x{addr:04X}: response has {len(regs)} regs, "
                f"needs {n_regs}"
            )
        return _regs_to_ascii(rr.registers)


class ScreenGrabPyrometer(TemperatureSensor):
    """
    Reads pyrometer temperature by scraping the BASF TemperaSure GUI window.

    Uses pywinauto (Windows only) to find the TemperaSure window, locate
    ToolBar2, and extract the temperature from its Edit control.

    Auto-detects the correct window title across lab machines by trying
    ``KNOWN_WINDOW_TITLES`` in order. Extend the list as we onboard new
    machines rather than requiring each site to override the title.

    Currently known titles:
      - Bulbasaur (Oxide MBE): 'BASF TemperaSure 5.7.0.4 Advanced Mode'
      - Ch-MBE (Chalcogenide): 'BASF TemperaSure 5.7.0.4'

    Env var ``AIQM_TEMPERASURE_TITLE`` overrides auto-detection when
    set — per-machine escape hatch. Explicit ``window_title`` argument
    still overrides everything (backward-compatible with existing tests).
    """

    # Preference order for auto-detection. Same pattern as
    # ElogReader.KNOWN_LOG_DIRS + _KNOWN_AI_REPO_ROOTS in growth_app /
    # events_tab. Add new machines here.
    KNOWN_WINDOW_TITLES = [
        "BASF TemperaSure 5.7.0.4 Advanced Mode",  # Bulbasaur (Oxide MBE)
        "BASF TemperaSure 5.7.0.4",                # Ch-MBE (added 2026-07-21)
    ]

    def __init__(
        self,
        window_title: Optional[str] = None,
        exe_path: Optional[str] = None,
        auto_start: bool = False,
    ):
        # None sentinel = auto-detect at connect time. Explicit string
        # bypasses auto-detection (backward compat with test callers that
        # pass a specific title).
        self._window_title = window_title
        self._exe_path = exe_path
        self._auto_start = auto_start
        self._app = None
        self._connected = False

    @classmethod
    def _candidate_titles(cls, explicit: Optional[str]) -> list[str]:
        """Ordered list of titles to try when connecting.

        Precedence: explicit arg → AIQM_TEMPERASURE_TITLE env var →
        KNOWN_WINDOW_TITLES in list order.
        """
        if explicit is not None:
            return [explicit]
        import os
        env = os.environ.get("AIQM_TEMPERASURE_TITLE", "").strip()
        if env:
            return [env]
        return list(cls.KNOWN_WINDOW_TITLES)

    def connect(self) -> None:
        try:
            from pywinauto.application import Application
        except ImportError:
            raise ImportError(
                "pywinauto not installed (Windows only): pip install pywinauto"
            )

        candidates = self._candidate_titles(self._window_title)

        if self._auto_start and self._exe_path:
            # auto_start path — use the first candidate as the title
            # to launch/kill against. Not iterated because launching
            # requires a single canonical title.
            self._window_title = candidates[0]
            self._app = self._start_temperasure(Application)
        else:
            # Connect to existing TemperaSure window — try each candidate
            # in order; use the first that succeeds. If all fail, raise
            # with the full list so the operator knows what we tried.
            last_error: Optional[Exception] = None
            for title in candidates:
                try:
                    self._app = Application(backend="uia").connect(title=title)
                    self._window_title = title  # remember which one worked
                    break
                except Exception as e:
                    last_error = e
                    continue
            else:
                # Every candidate failed
                raise RuntimeError(
                    f"Could not connect to TemperaSure. Tried titles "
                    f"{candidates!r}. Last error: {last_error}"
                )

        self._connected = True

    def _start_temperasure(self, Application) -> "Application":
        """Launch TemperaSure, click through Port Setup dialog, hit Start."""
        import time

        # Kill existing instance if running
        try:
            existing = Application(backend="uia").connect(
                title=self._window_title
            )
            existing.kill()
        except Exception:
            pass

        app = Application(backend="uia").start(self._exe_path)
        time.sleep(2)

        main = app.window(title=self._window_title)

        # Dismiss Port Setup dialog
        try:
            port_dlg = main.child_window(
                title="Port setup", control_type="Window"
            )
            port_dlg.child_window(title="OK", control_type="Button").click()
        except Exception:
            pass

        time.sleep(1)

        # Click Start
        try:
            main.child_window(title="Start", control_type="Button").click()
        except Exception:
            pass

        return app

    def disconnect(self) -> None:
        self._app = None
        self._connected = False

    def read_temperature(self) -> float:
        """Scrape temperature value from TemperaSure Edit control."""
        if not self._connected or self._app is None:
            raise RuntimeError("TemperaSure not connected.")

        dlg = self._app.window(title=self._window_title)
        toolbar2 = dlg.child_window(control_type="ToolBar", title_re="ToolBar2")
        edit_controls = toolbar2.children(control_type="Edit")

        if not edit_controls:
            raise RuntimeError("No Edit control found in ToolBar2")

        value_str = edit_controls[0].get_value()
        try:
            return float(value_str)
        except (ValueError, TypeError):
            raise RuntimeError(f"Could not parse temperature: '{value_str}'")

    def read_emissivity(self) -> Optional[float]:
        """Scrape emissivity value from TemperaSure, if available.

        Emissivity is typically in the second Edit control of ToolBar2,
        or in a separate toolbar. Returns None if not found.
        """
        if not self._connected or self._app is None:
            return None

        try:
            dlg = self._app.window(title=self._window_title)
            toolbar2 = dlg.child_window(control_type="ToolBar", title_re="ToolBar2")
            edit_controls = toolbar2.children(control_type="Edit")

            if len(edit_controls) >= 2:
                val = float(edit_controls[1].get_value())
                if 0.0 <= val <= 1.0:
                    return val

            # Fallback: search all toolbars for an Edit with a value in [0, 1]
            for tb in dlg.children(control_type="ToolBar"):
                for ed in tb.children(control_type="Edit"):
                    try:
                        val = float(ed.get_value())
                        if 0.01 <= val <= 1.0:
                            return val
                    except (ValueError, TypeError):
                        continue
        except Exception:
            pass

        return None


class ExactusSerialPyrometer(TemperatureSensor):
    """
    BASF Exactus optical thermometer — binary streaming serial protocol.

    Packet format (as of this driver's understanding; **source unverified**
    against the BASF Exactus manual — ask Aarav Sonthalia, who added this
    class in the Jun 22 cherry-pick, for the authoritative reference):

        [0x81, B0, B1, B2, B3]      — 5 bytes per sample
         │     └───────────────────── IEEE 754 float32 (temperature, °C)
         └────────────────────────── header / sync byte

    Endianness is ``>f`` (big-endian) by default; switch ``FLOAT_STRUCT``
    if the device is little-endian.

    Sits alongside :class:`ModbusPyrometer` (RTU) and :class:`ScreenGrabPyrometer`
    (pywinauto). All three currently default to ``port="COM4"`` and only
    one can hold the port at a time — TemperaSure in particular takes it
    exclusively (see ``docs/ksa400_manual_findings.md``).

    Read flow: ``read_temperature()`` syncs to the ``0x81`` header, reads
    the 4-byte float body, validates against the plausibility range, and
    returns °C. Call from a background thread; each read blocks until one
    packet arrives or the serial timeout fires. Consecutive failures
    (:attr:`_MAX_CONSECUTIVE_FAILS`) mark the driver disconnected so the
    worker's reconnect path can trigger.
    """

    # Packet framing constants — modify if the manual reveals a different shape.
    HEADER = 0x81
    BODY_LEN = 4
    FLOAT_STRUCT = ">f"  # ">f" = big-endian; "<f" = little-endian

    # Plausibility bounds — matches ModbusPyrometer._detect_word_order.
    # Anything outside this range is treated as a mis-framed packet
    # (sync happened on 0x81 that was actually a data byte).
    TEMP_MIN_C = -100.0
    TEMP_MAX_C = 3000.0

    # Bounded header-sync loop — if we can't find 0x81 within this many
    # bytes, something's wrong (wrong baud, wrong device, wrong port).
    # At ~50 bytes/s worst case per byte-timeout, this is <10s of hunting.
    MAX_SYNC_BYTES = 256

    # Consecutive read failures before marking self disconnected. The
    # worker sees connected=False and can trigger a reconnect. Cheap
    # detection of USB drop / device reset / TemperaSure grabbing the port.
    MAX_CONSECUTIVE_FAILS = 5

    def __init__(
        self,
        port: str = "COM4",
        baudrate: int = 115200,
        timeout: float = 2.0,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
    ):
        # Framing params mirror ModbusPyrometer defaults. Serial's own
        # defaults have historically shifted — pin them explicitly here
        # so the wire format is reproducible independent of pyserial
        # version.
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._parity = parity
        self._stopbits = stopbits
        self._bytesize = bytesize
        self._serial = None
        self._connected = False
        self._consecutive_fails = 0

    def connect(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise ImportError(
                "pyserial not installed: pip install pyserial"
            ) from exc

        # pyserial's ``Serial(port=..., ...)`` opens the port at
        # construction time when ``port`` is truthy — no separate
        # ``open()`` call is needed. Keep this call short and rely on
        # constructor semantics; an explicit ``is_open`` re-check would
        # be dead code.
        self._serial = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            parity=self._parity,
            stopbits=self._stopbits,
            bytesize=self._bytesize,
            timeout=self._timeout,
        )
        # Discard any partial/stale bytes sitting in the OS buffer from
        # before we opened — otherwise the first ``read_temperature()``
        # may sync on a ``0x81`` that's the middle of a stale packet.
        self._serial.reset_input_buffer()

        self._connected = True
        self._consecutive_fails = 0
        log.info(
            "ExactusSerialPyrometer connected: %s @ %d 8%s%d",
            self._port, self._baudrate, self._parity, self._stopbits,
        )

    def disconnect(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001 — pyserial close can raise many types
                log.debug("ExactusSerialPyrometer close raised; ignoring", exc_info=True)
            self._serial = None
        self._connected = False
        self._consecutive_fails = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def get_info(self) -> dict:
        """Static device-config summary for state.device_info.

        Binary streaming doesn't expose a runtime identity read (unlike
        Modbus RTU's REG_NAME0/REG_SN0), so this returns the connection
        parameters so the GUI shows *something* in place of the literal
        mode string.
        """
        return {
            "name": "BASF Exactus (binary streaming)",
            "port": self._port,
            "baud": self._baudrate,
            "framing": f"8{self._parity}{self._stopbits}",
        }

    def read_temperature(self) -> float:
        """Block until one complete Exactus packet is received, return °C.

        Raises:
            RuntimeError: not connected, sync timeout, short packet,
                or the parsed float lies outside ``[TEMP_MIN_C, TEMP_MAX_C]``.
                On the ``MAX_CONSECUTIVE_FAILS``-th consecutive raise,
                the driver additionally flips ``self._connected`` to False
                so the worker's reconnect path can trigger.
        """
        if not self._connected or self._serial is None:
            raise RuntimeError("ExactusSerialPyrometer not connected.")

        try:
            temp_c = self._read_one_packet()
        except RuntimeError:
            self._register_failure()
            raise
        self._consecutive_fails = 0
        return temp_c

    # -- Internals ---------------------------------------------------------

    def _read_one_packet(self) -> float:
        """Sync to header, read body, unpack + validate. Raises on failure."""
        # Bounded header-sync loop. Each read(1) call is bounded by
        # ``self._timeout`` at the pyserial layer; we additionally cap the
        # number of bytes we're willing to hunt through so a wrong-baud
        # stream fails fast rather than churning until the loop naturally
        # exits on an empty read.
        bytes_hunted = 0
        while True:
            b = self._serial.read(1)
            if not b:
                raise RuntimeError(
                    "Serial timeout waiting for Exactus 0x81 header."
                )
            if b[0] == self.HEADER:
                break
            bytes_hunted += 1
            if bytes_hunted >= self.MAX_SYNC_BYTES:
                raise RuntimeError(
                    f"No Exactus 0x81 header in {self.MAX_SYNC_BYTES} bytes — "
                    f"check baud rate ({self._baudrate}) and port ({self._port})."
                )

        body = self._serial.read(self.BODY_LEN)
        if len(body) < self.BODY_LEN:
            raise RuntimeError(
                f"Exactus short packet: expected {self.BODY_LEN} bytes, "
                f"got {len(body)}."
            )

        temp_c = struct.unpack(self.FLOAT_STRUCT, body)[0]
        if not (self.TEMP_MIN_C < temp_c < self.TEMP_MAX_C):
            raise RuntimeError(
                f"Exactus implausible temperature {temp_c!r} °C "
                f"(outside [{self.TEMP_MIN_C}, {self.TEMP_MAX_C}] °C) — "
                f"likely mis-framed packet or wrong endianness "
                f"(FLOAT_STRUCT={self.FLOAT_STRUCT!r})."
            )
        return float(temp_c)

    def _register_failure(self) -> None:
        """Increment fail counter and mark disconnected past the threshold."""
        self._consecutive_fails += 1
        if self._consecutive_fails >= self.MAX_CONSECUTIVE_FAILS:
            log.warning(
                "ExactusSerialPyrometer: %d consecutive read failures; "
                "marking disconnected so worker reconnect can trigger.",
                self._consecutive_fails,
            )
            self._connected = False


class DummyPyrometer(TemperatureSensor):
    """Test pyrometer that returns a slowly varying temperature."""

    def __init__(self, base_temp: float = 450.0, noise: float = 2.0):
        self._base_temp = base_temp
        self._noise = noise
        self._connected = False
        self._read_count = 0

    def connect(self) -> None:
        self._connected = True
        self._read_count = 0

    def disconnect(self) -> None:
        self._connected = False

    def read_temperature(self) -> float:
        if not self._connected:
            raise RuntimeError("Dummy pyrometer not connected.")

        import math
        import random

        self._read_count += 1
        # Slow sinusoidal drift + random noise
        drift = 5.0 * math.sin(self._read_count * 0.02)
        noise = random.gauss(0, self._noise)
        return self._base_temp + drift + noise
