"""Vimba X / vmbpy camera feature probe — discovery for GUI exposure control.

Answers one question the Vimba X Viewer cannot: what are the *GenICam feature
names* behind the Viewer's Brightness sliders, and are they writable from
vmbpy on this camera's firmware?

The Manta G-033B is a legacy AVT GigE camera. Its classic feature set uses
``ExposureTimeAbs`` (float, us) and ``GainRaw``/``Gain``; the SFNC standard
that newer cameras follow uses ``ExposureTime``/``Gain``. Vimba X may present
either, depending on the GenTL producer and firmware (this camera reports
firmware 00.01.44.18241). Hardcoding the wrong name in drivers/rheed_camera.py
would fail at runtime on the lab machine only, so this script establishes the
truth before any driver change is written.

SAFETY LADDER (same pattern as scripts/pyrometer_force_modbus.py):

  Phase 1 (default)  READ-ONLY. Enumerates every feature with its type,
                     access mode, range, increment, unit and current value.
                     Touches nothing. Safe to run any time the camera is free.

  Phase 2 (gated)    Requires --i-am-doing-a-write. Sets exposure to a test
                     value, reads it back, then RESTORES the original in a
                     finally block. Never calls UserSetSave/UserSetStore, so
                     every write is RAM-only and reverts on camera power-cycle
                     even if this script is killed mid-run.

PREREQUISITE: close the Vimba X Viewer first. It holds the camera in Full
access; while it is open this script can only get Read access and every
feature will report writable=False, which looks identical to "the firmware
forbids it". The script warns when it lands in Read mode for this reason.

Usage:
    python scripts/vimba_feature_probe.py
    python scripts/vimba_feature_probe.py --output logs/vimba_features.json
    python scripts/vimba_feature_probe.py --filter exposure
    python scripts/vimba_feature_probe.py --i-am-doing-a-write --test-exposure-us 150000
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# Feature-name candidates for the two knobs the growers asked for. Ordered
# most-likely-first for this camera family; the probe reports which exist
# rather than assuming any of them do.
# Settable exposure *time* features only. Kept strictly separate from the
# contextual ones below: resolution picks the first present entry, and if
# ExposureAuto were in this tuple a camera lacking all three time features
# would resolve the enum and Phase 2 would call ExposureAuto.set(250000.0).
# That cannot happen on this Manta (ExposureTimeAbs is present) but it would
# silently break the probe's cross-camera claim.
EXPOSURE_VALUE_CANDIDATES = (
    # CONFIRMED 2026-08-06 from the Vimba X Viewer "All" tab on this camera:
    # Camera > Controls > Exposure > ExposureTimeAbs = 300000. The SFNC
    # spelling is absent on this firmware; the fallbacks keep the probe
    # honest on other cameras.
    "ExposureTimeAbs",      # legacy AVT GigE — the real name here
    "ExposureTime",         # SFNC standard — not present on Manta 00.01.44
    "ExposureTimeRaw",
)
# Read and reported, never written, never resolved as "the exposure feature".
EXPOSURE_CONTEXT_CANDIDATES = (
    "ExposureAuto",         # Off — must stay Off for a write test to mean anything
    "ExposureAutoTarget",
    "ExposureMode",         # Timed
)
EXPOSURE_CANDIDATES = EXPOSURE_VALUE_CANDIDATES + EXPOSURE_CONTEXT_CANDIDATES
GAIN_CANDIDATES = (
    "Gain",
    "GainRaw",
    "GainAuto",
    "GainSelector",
)
# Read for context: these shape how a raw pixel value maps to the intensity
# the GUI plots. BlackLevel is an additive pedestal (35 DN on this camera as
# of 2026-08-06) and Gamma a non-linearity — both matter for whether the
# RHEED intensity trend can ever be a physical quantity.
CONTEXT_CANDIDATES = (
    "BlackLevel",
    "BlackLevelRaw",
    "Gamma",
    "PixelFormat",
    "Width",
    "Height",
    # AcquisitionFrameRateLimit is read-only and equals 1/exposure: the
    # camera's own statement of what it can physically deliver. Observed
    # 2026-08-06 as 3.3323 fps against a configured AcquisitionFrameRateAbs
    # of 88.4017 — i.e. the configured rate is unreachable at 300 ms
    # exposure. VmbCamera.trigger_hz must stay under the limit.
    "AcquisitionFrameRateAbs",
    "AcquisitionFrameRateLimit",
    "AcquisitionFrameRate",
    # SensorBits confirms VmbCamera's _max_value = (1 << bit_depth) - 1
    # assumption programmatically instead of by datasheet. Observed 12.
    "SensorBits",
    "SensorType",
    "DeviceFirmwareVersion",
    "DeviceModelName",
    "DeviceSerialNumber",
    "DevicePartNumber",
    "DeviceVendorName",
)


def _safe(owner, method_name: str, default=None):
    """Call ``owner.method_name()``, tolerating a missing OR a raising method.

    The attribute lookup must happen INSIDE this function. Passing a bound
    method (``_safe(feature.get_available_entries)``) evaluates the attribute
    at the call site, so an AttributeError escapes before the try block ever
    runs. That is exactly how the 2026-08-06 Ch-MBE Full-access run died on
    the very first feature: get_available_entries is EnumFeature-only, and
    FirmwareVerMajor is an IntFeature.

    vmbpy's feature classes are a ragged hierarchy — get_range/get_increment
    exist on Int/Float only, get_available_entries on Enum only, get() on
    everything except Command. Surveying all 168 means every accessor is
    conditional, so absence must be as survivable as failure.
    """
    fn = getattr(owner, method_name, None)
    if fn is None or not callable(fn):
        return default
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — surveying unknown SDK surface
        return default if default is not None else f"<unavailable: {exc}>"


def describe_feature(feature) -> dict[str, Any]:
    """Collect everything interesting about one feature, defensively."""
    name = _safe(feature, "get_name", "<unknown>")
    info: dict[str, Any] = {
        "name": name,
        "display_name": _safe(feature, "get_display_name", ""),
        "type": type(feature).__name__,
        "unit": _safe(feature, "get_unit", ""),
        "tooltip": _safe(feature, "get_tooltip", ""),
    }

    access = _safe(feature, "get_access_mode", None)
    if isinstance(access, tuple) and len(access) == 2:
        info["readable"], info["writable"] = bool(access[0]), bool(access[1])
    else:
        info["readable"], info["writable"] = None, None
        info["access_error"] = str(access)

    # Command features have no value; reading one would execute nothing but
    # does raise. Skip by type name rather than by try/except so the report
    # distinguishes "command" from "read failed".
    if "Command" not in info["type"]:
        value = _safe(feature, "get", None)
        info["value"] = value if _is_jsonable(value) else str(value)

    rng = _safe(feature, "get_range", None)
    if isinstance(rng, tuple) and len(rng) == 2:
        info["min"], info["max"] = _jsonable(rng[0]), _jsonable(rng[1])

    inc = _safe(feature, "get_increment", None)
    if inc is not None and not isinstance(inc, str):
        info["increment"] = _jsonable(inc)

    # Enum features carry the legal strings — needed to know whether
    # ExposureAuto accepts "Off"/"Once"/"Continuous" verbatim.
    entries = _safe(feature, "get_available_entries", None)
    if isinstance(entries, (list, tuple)):
        info["entries"] = [str(e) for e in entries]

    return info


def _is_jsonable(value: Any) -> bool:
    return isinstance(value, (int, float, str, bool, type(None)))


def _jsonable(value: Any) -> Any:
    return value if _is_jsonable(value) else str(value)


def open_camera(vmbpy, vmb, index: int, mode: str):
    """Open camera `index` in `mode`, returning (camera, negotiated_mode).

    Mirrors drivers/rheed_camera.py: Full is tried first unless the caller
    pins a mode, because feature writes need Full and a silent Read fallback
    would make a writable camera look read-only.
    """
    cams = vmb.get_all_cameras()
    if not cams:
        raise RuntimeError(
            "No cameras found. Check the GigE link (this camera is on a "
            "link-local 169.254.x.x address, so it needs a direct NIC) and "
            "that the Vimba X transport layer is installed."
        )
    if index >= len(cams):
        raise RuntimeError(f"camera_index {index} out of range; found {len(cams)}")

    cam = cams[index]
    attempts = ("full", "read") if mode == "auto" else (mode,)
    last_error: Optional[Exception] = None
    for attempt in attempts:
        try:
            cam.set_access_mode(getattr(vmbpy.AccessMode, attempt.capitalize()))
            cam.__enter__()
            return cam, attempt
        except Exception as exc:  # noqa: BLE001 — SDK raises several types
            last_error = exc
            continue
    raise RuntimeError(f"Could not open camera in {attempts}: {last_error}")


def find_first_present(features: dict[str, dict], names) -> Optional[str]:
    for candidate in names:
        if candidate in features:
            return candidate
    return None


def write_test(feature, test_value: float, features: dict[str, dict]) -> dict[str, Any]:
    """Set → read back → restore, with preconditions checked first.

    SCOPE: this proves the camera *register* accepted a value and reported it
    back. It does NOT start acquisition, so it says nothing about whether
    integration time actually changed, whether image brightness responded, or
    whether in-stream writes are safe from the GUI's thread. Those need a
    separate acquisition phase — do not read a pass here as end-to-end proof.

    Deliberately does NOT call UserSetSave/UserSetStore: the write stays in
    volatile camera RAM, so a power-cycle reverts it regardless of how this
    script exits. That is the real safety net — a finally block cannot help
    after a SIGKILL, an interpreter crash, or a dropped GigE link.
    """
    result: dict[str, Any] = {
        "requested": test_value,
        "original": None,
        "readback": None,
        "restored": None,
        "accepted": False,
        "restore_verified": False,
        "skipped_reason": "",
        "error": "",
    }

    # Precondition: auto-exposure must be off, or the camera overwrites the
    # value we just set and the readback measures the auto loop, not our write.
    auto = features.get("ExposureAuto", {}).get("value")
    if auto is not None and str(auto).split("_")[-1].lower() != "off":
        result["skipped_reason"] = (
            f"ExposureAuto is {auto!r}, not Off — a write test would race the "
            "auto-exposure loop. Set it Off in the Viewer first."
        )
        return result

    original = feature.get()
    result["original"] = _jsonable(original)

    # Validate against the feature's own range/increment BEFORE writing,
    # rather than discovering rejection from an SDK exception.
    rng = _safe(feature, "get_range", None)
    increment = _safe(feature, "get_increment", None)
    if isinstance(rng, tuple) and len(rng) == 2:
        low, high = float(rng[0]), float(rng[1])
        result["range"] = [low, high]
        if not (low <= test_value <= high):
            result["skipped_reason"] = (
                f"requested {test_value} outside device range [{low}, {high}]"
            )
            return result
    if isinstance(increment, (int, float)) and increment:
        # Quantise to the device grid so the readback comparison is exact
        # rather than "close enough".
        quantised = round(test_value / float(increment)) * float(increment)
        result["quantised_request"] = quantised
        test_value = quantised

    try:
        feature.set(test_value)
        readback = float(feature.get())
        result["readback"] = readback
        tolerance = abs(float(increment)) if isinstance(increment, (int, float)) else 1.0
        result["accepted"] = abs(readback - test_value) <= max(tolerance, 1.0)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            feature.set(original)
            restored = float(feature.get())
            result["restored"] = restored
            tolerance = abs(float(increment)) if isinstance(increment, (int, float)) else 1.0
            result["restore_verified"] = (
                abs(restored - float(original)) <= max(tolerance, 1.0)
            )
        except Exception as exc:  # noqa: BLE001
            result["restored"] = f"<RESTORE FAILED: {exc}>"
            result["restore_verified"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument(
        "--access-mode", choices=("auto", "full", "read"), default="auto",
    )
    parser.add_argument(
        "--filter", default="",
        help="Case-insensitive substring; limits the printed table only. "
             "The JSON report always contains every feature.",
    )
    parser.add_argument("--output", default="", help="Write full JSON report here")
    parser.add_argument(
        "--i-am-doing-a-write", action="store_true",
        help="Enable Phase 2: the set/readback/restore exposure test.",
    )
    parser.add_argument(
        "--trigger-hz", type=float, default=1.0,
        help="VmbCamera.trigger_hz to sanity-check against the camera's "
             "achievable frame rate (default 1.0, the driver's default).",
    )
    parser.add_argument(
        "--test-exposure-us", type=float, default=250000.0,
        help="Exposure to set during Phase 2. Default 250000 us is a "
             "deliberately small perturbation from the 2026-08-06 observed "
             "300000 us — large enough to be unambiguous in a readback, "
             "small enough not to disturb the beam picture much.",
    )
    parser.add_argument(
        "--require-serial", default="",
        help="Abort unless the opened camera reports this serial. Phase 2 "
             "writes to whatever --camera-index resolves to, and enumeration "
             "order is not guaranteed stable; on Ch-MBE pass 50-0503464907.",
    )
    parser.add_argument(
        "--require-id", default="",
        help="Abort unless the opened camera reports this device ID "
             "(Ch-MBE: DEV_000F314E8D67).",
    )
    args = parser.parse_args()

    try:
        import vmbpy
    except ImportError:
        print(
            "vmbpy not installed. On the lab machine this ships with the "
            "Vimba X SDK; confirm the venv can see it.", file=sys.stderr,
        )
        return 1

    exit_code = 0
    report: dict[str, Any] = {
        "probed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vmbpy_version": getattr(vmbpy, "__version__", "<unknown>"),
        "phase_2_attempted": bool(args.i_am_doing_a_write),
    }

    try:
        exit_code = _probe(vmbpy, args, report, exit_code)
    finally:
        # Always emit whatever was gathered. A crash mid-sweep still costs a
        # Viewer close, a GUI close and ~30 s of discovery to repeat, so the
        # partial report is worth more than a clean stack trace alone.
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, sort_keys=True)
            print(f"\nReport written to {args.output}")
    return exit_code


def _probe(vmbpy, args, report: dict[str, Any], exit_code: int) -> int:
    with vmbpy.VmbSystem.get_instance() as vmb:
        cam, mode = open_camera(vmbpy, vmb, args.camera_index, args.access_mode)
        report["access_mode"] = mode
        try:
            report["camera"] = {
                "id": _safe(cam, "get_id", ""),
                "model": _safe(cam, "get_model", ""),
                "serial": _safe(cam, "get_serial", ""),
                "interface": _safe(cam, "get_interface_id", ""),
            }
            print(f"Camera : {report['camera']['model']} ({report['camera']['serial']})")
            print(f"Access : {mode.upper()}")

            # Identity lock. --camera-index is positional and enumeration
            # order is not guaranteed across reboots or NIC changes, so a
            # write must never be aimed by index alone.
            for field, expected in (
                ("serial", args.require_serial), ("id", args.require_id),
            ):
                actual = str(report["camera"].get(field, ""))
                if expected and expected not in actual:
                    print(
                        f"\nABORT: camera {field} is {actual!r}, expected "
                        f"{expected!r}. Refusing to touch the wrong camera.",
                        file=sys.stderr,
                    )
                    return 3
            if args.i_am_doing_a_write and not (args.require_serial or args.require_id):
                print(
                    "\nABORT: Phase 2 requires --require-serial and/or "
                    "--require-id so the write is aimed at a verified camera, "
                    "not at whatever --camera-index happened to resolve.",
                    file=sys.stderr,
                )
                return 3
            if mode != "full":
                print(
                    "\n  !! Opened in READ mode. Every feature below will report\n"
                    "     writable=False regardless of what the firmware allows.\n"
                    "     Close the Vimba X Viewer and re-run for a valid answer.\n"
                )

            # Per-feature isolation. describe_feature() is already defensive,
            # but this probe exists to survey an SDK surface we do not fully
            # know — one unanticipated feature type must never cost the whole
            # run. Lab runs are expensive: they need the Viewer closed, the
            # GUI closed, and ~30 s of discovery. Losing 167 good records to
            # one bad one (as happened 2026-08-06) is the failure to avoid.
            features: dict[str, dict] = {}
            describe_errors: dict[str, str] = {}
            for feature in cam.get_all_features():
                fallback_name = _safe(feature, "get_name", "<unknown>")
                try:
                    info = describe_feature(feature)
                    features[info["name"]] = info
                except Exception as exc:  # noqa: BLE001
                    describe_errors[str(fallback_name)] = (
                        f"{type(exc).__name__}: {exc}"
                    )
            if describe_errors:
                report["describe_errors"] = describe_errors
                print(
                    f"\n{len(describe_errors)} feature(s) could not be "
                    f"described; see 'describe_errors' in the JSON. The rest "
                    f"of the report is complete."
                )
            report["feature_count"] = len(features)
            report["features"] = features

            exposure_name = find_first_present(features, EXPOSURE_VALUE_CANDIDATES)
            gain_name = find_first_present(features, GAIN_CANDIDATES)
            report["resolved"] = {
                "exposure_feature": exposure_name,
                "gain_feature": gain_name,
            }

            print(f"\n{len(features)} features enumerated.\n")
            _print_group("EXPOSURE", EXPOSURE_CANDIDATES, features)
            _print_group("GAIN", GAIN_CANDIDATES, features)
            _print_group("CONTEXT", CONTEXT_CANDIDATES, features)

            if args.filter:
                needle = args.filter.lower()
                matches = {
                    n: f for n, f in features.items() if needle in n.lower()
                }
                _print_group(f"FILTER '{args.filter}'", tuple(matches), features)

            print("\n--- VERDICT ---")
            print(f"Exposure feature : {exposure_name or 'NOT FOUND'}")
            print(f"Gain feature     : {gain_name or 'NOT FOUND'}")
            report["frame_rate_check"] = _frame_rate_check(
                features, exposure_name, args.trigger_hz,
            )
            for line in report["frame_rate_check"]["lines"]:
                print(line)
            if exposure_name:
                writable = features[exposure_name].get("writable")
                print(f"Exposure writable: {writable}")
                if writable and not args.i_am_doing_a_write:
                    print(
                        "\nExposure appears writable. Re-run with "
                        "--i-am-doing-a-write to verify a real set/readback "
                        "(the value is restored afterwards)."
                    )

            if args.i_am_doing_a_write:
                if not exposure_name:
                    print("\nPhase 2 skipped: no exposure feature found.")
                elif mode != "full":
                    print("\nPhase 2 skipped: needs Full access.")
                else:
                    print(f"\n--- PHASE 2: write test on {exposure_name} ---")
                    outcome = write_test(
                        getattr(cam, exposure_name),
                        args.test_exposure_us,
                        features,
                    )
                    report["write_test"] = outcome
                    for key, value in outcome.items():
                        print(f"  {key:18s}: {value}")
                    print(
                        "\n  SCOPE: this proves the register accepted and "
                        "reported back a value.\n"
                        "  It does NOT prove integration time changed or that "
                        "image brightness responded —\n"
                        "  that needs a separate acquisition phase."
                    )
                    if outcome["skipped_reason"]:
                        print(f"\n  SKIPPED: {outcome['skipped_reason']}")
                    elif not outcome["restore_verified"]:
                        print(
                            "\n  *** RESTORATION NOT VERIFIED ***\n"
                            f"  The camera may still be at {outcome['requested']} us.\n"
                            "  Check ExposureTimeAbs in the Vimba X Viewer, or "
                            "power-cycle the camera\n"
                            "  (the write was never saved to a user set, so a "
                            "power-cycle reverts it).",
                            file=sys.stderr,
                        )
                        exit_code = 4
        finally:
            cam.__exit__(None, None, None)

    # The report is written by main()'s finally, so it survives an exception
    # raised anywhere above. Do not duplicate the write here.
    return exit_code


def _frame_rate_check(
    features: dict[str, dict], exposure_name: Optional[str], trigger_hz: float,
) -> dict[str, Any]:
    """Cross-check exposure against what the camera can actually deliver.

    A GUI exposure slider is a frame-rate control in disguise: the sensor
    cannot start a new integration until the previous one ends, so the
    achievable rate is bounded by 1/exposure. AcquisitionFrameRateLimit is
    the camera's own read-only statement of that bound.

    FAILURE MODE (corrected 2026-08-06). Over-triggering the DIRECT Vimba
    path does not raise a timeout. window_capture.py's 5 s stale timeout
    belongs to ScreenGrabCamera (the WGC/kSA-window backend); VmbCamera
    never calls it. VmbCamera.read_frame() returns ``_latest_frame.copy()``
    unconditionally, with no age check and no frame identity, and VmbCamera
    exposes no ``last_capture`` — so RheedCameraWorker takes its else-branch
    and synthesises ``frame_age_ms = 0.0`` with ``capture_sequence =
    frame_count``.

    The real consequence is therefore silent, not loud: the same cached
    image is re-served and counted as a new frame. That inflates worker FPS,
    feeds duplicates to the intensity trend and the classifier, and corrupts
    change-detector inputs. Nothing errors. Until VmbCamera carries a camera
    frame ID and capture timestamp, this check is the only warning available.
    """
    out: dict[str, Any] = {"lines": [], "ok": None}
    exposure_info = features.get(exposure_name or "", {})
    exposure_us = exposure_info.get("value")
    limit = features.get("AcquisitionFrameRateLimit", {}).get("value")
    configured = features.get("AcquisitionFrameRateAbs", {}).get("value")

    # The 1e6/exposure arithmetic is only valid if the feature really is in
    # microseconds. ExposureTimeRaw on some firmwares is in device ticks, and
    # silently reporting a wrong fps ceiling is worse than reporting none.
    unit = str(exposure_info.get("unit", "") or "")
    unit_ok = unit.strip().lower() in ("us", "µs", "microsecond", "microseconds", "")
    out["exposure_unit"] = unit
    if not unit_ok:
        out["lines"].append(
            f"Exposure unit    : {unit!r} is not microseconds — skipping the "
            "fps derivation (would be meaningless)."
        )
        exposure_us = None

    if isinstance(exposure_us, (int, float)) and exposure_us > 0:
        implied = 1_000_000.0 / float(exposure_us)
        out["exposure_us"] = float(exposure_us)
        out["implied_max_fps"] = round(implied, 4)
        out["lines"].append(
            f"Exposure         : {exposure_us:.0f} us "
            f"-> implies <= {implied:.2f} fps"
        )
    if isinstance(limit, (int, float)):
        out["frame_rate_limit"] = float(limit)
        out["lines"].append(f"Camera fps limit : {limit:.4f} (read-only)")
    if isinstance(configured, (int, float)):
        out["frame_rate_configured"] = float(configured)
        note = ""
        if isinstance(limit, (int, float)) and configured > limit:
            note = "  <-- configured above the limit; unreachable"
        out["lines"].append(f"Configured fps   : {configured:.4f}{note}")

    bound = limit if isinstance(limit, (int, float)) else out.get("implied_max_fps")
    if isinstance(bound, (int, float)):
        headroom = bound / trigger_hz if trigger_hz > 0 else float("inf")
        out["trigger_hz"] = trigger_hz
        out["headroom_x"] = round(headroom, 2)
        out["ok"] = trigger_hz <= bound
        verdict = (
            "OK" if out["ok"]
            else "TOO FAST — expect silently re-served cached frames "
                 "counted as new (no error is raised)"
        )
        out["lines"].append(
            f"VmbCamera trigger: {trigger_hz:.2f} Hz vs {bound:.2f} fps "
            f"achievable ({headroom:.1f}x headroom) -> {verdict}"
        )
    return out


def _print_group(title: str, names, features: dict[str, dict]) -> None:
    print(f"[{title}]")
    found = False
    for name in names:
        info = features.get(name)
        if info is None:
            continue
        found = True
        bounds = ""
        if "min" in info:
            bounds = f"  range=[{info['min']}, {info['max']}]"
            if "increment" in info:
                bounds += f" inc={info['increment']}"
        entries = f"  entries={info['entries']}" if "entries" in info else ""
        print(
            f"  {name:26s} {str(info.get('type', '')):18s} "
            f"r={info.get('readable')} w={info.get('writable')} "
            f"value={info.get('value')!r} {info.get('unit', '')}{bounds}{entries}"
        )
    if not found:
        print("  (none of these names exist on this camera)")
    print()


if __name__ == "__main__":
    sys.exit(main())
