# Ch-MBE camera-exposure acceptance — `feat/ombe-exposure-integration`

Hardware acceptance for the grower exposure control on the **Chalcogenide
MBE / Omicron PC**. Companion to `acceptance_camera_exposure_ombe.md`.

**Do not substitute one document for the other.** The chambers differ in
the two ways that matter most here:

| | O-MBE (Bulbasaur) | Ch-MBE (Omicron) |
|---|---|---|
| `camera_exposure_us` | `None` | **`300_000.0`** |
| Control opens on | *Keep current* — no write | **A value — ARM writes 300 ms** |
| kSA | normally open, shares the camera | **off** (2026-08-05 decision) |
| Access mode in practice | Read is common | **Full** |

So on Ch-MBE the **first ARM writes to the camera**, by design and by
config. That makes §1 and §2 below the load-bearing steps, and it is the
opposite of the O-MBE default. Every write is volatile — `UserSetSave` is
never called — so a power-cycle restores the stored user set.

Camera identity for this chamber: serial **50-0503464907**, device
**DEV_000F314E8D67**, Manta G-033B, firmware 00.01.44.18241.

---

## Setup

```powershell
conda activate ai4mbe-gui
cd C:\path\to\AIQM-Software-Hardware-Integration
git fetch origin
git checkout feat/ombe-exposure-integration
git rev-parse --short HEAD      # expect 18b8894 or later
$env:AIQM_CHAMBER = "chmbe"
$env:AIQM_CHAMBER                # confirm — growth_monitor_chmbe.py uses
                                 # setdefault, so a stale value survives
```

Record: commit `__________` · date `__________` · operator `__________`

Confirm kSA is closed and the Vimba X Viewer is closed. Either one holding
the camera forces Read access, and every writable bit reads False — which
looks identical to a firmware refusal.

---

## 1. Baseline, and the probe's first real run

```powershell
python scripts\vimba_feature_probe.py --require-serial 50-0503464907 --output D:\AIQM-evidence\chmbe_exposure_baseline.json
```

> This is also the **first hardware run of the fixed probe**. The
> 2026-08-06 attempt died on its first feature (`FirmwareVerMajor`,
> `_safe` bug) and produced no records at all; `e086a79` fixes it but has
> only ever been verified against mocks. If it crashes again, capture the
> traceback — a partial `report["describe_errors"]` is still written from
> `main()`'s `finally`.

RESULT — `ExposureTimeAbs` = `________` us (expect ~300000) ·
`ExposureAuto` = `________` (expect Off) · `ExposureMode` = `________` ·
`BlackLevel` = `________` (expect 35) ·
`AcquisitionFrameRateLimit` = `________` (expect ~3.33) ·
`GevTimestampTickFrequency` = `________`

Access mode reported: `________` — must be **Full**.

If `ExposureTimeAbs` is not ~300000, the config default in
`drivers/config.py` is stale and should be corrected before shipping.

## 2. The configured default applies on ARM — **WRITES**

Launch the GUI. The exposure control should already show **300 ms with
*Keep current* unchecked**, because Ch-MBE ships a configured value.

- [ ] Control opens at 300 ms, *Keep current* **unchecked**
- [ ] ARM succeeds
- [ ] Status bar: "Direct camera exposure confirmed: 300 ms"
- [ ] Re-probe: `ExposureTimeAbs` reads 300000

This is a no-op write in practice — the camera is already at 300 ms — which
makes it the safest possible first exercise of the write path.

## 3. *Keep current* still writes nothing

- [ ] Check *Keep current*, DISARM, re-ARM
- [ ] Slider and textbox grey out
- [ ] Re-probe: exposure unchanged
- [ ] Confirm no "exposure confirmed" status message for a fresh value

## 4. Two or three values visibly change brightness — **WRITES**

| Requested | Status bar | Image vs previous | Probe readback |
|---|---|---|---|
| 100 ms | `________` | `________` | `________` |
| 300 ms | `________` | `________` | `________` |
| 600 ms | `________` | `________` | `________` |

- [ ] Brightness increases monotonically
- [ ] Confirmed value is the **readback** — a small difference from the
      request means the device quantised to its increment grid, which is
      correct

## 5. Slider / textbox / mode gating

- [ ] Slider ↔ textbox track each other, range 1–999 ms
- [ ] Switching camera mode to `screengrab` greys out all three controls;
      switching back to `vimba` restores them

## 6. The ceiling, and the frame-rate reality check

Ch-MBE is where `AcquisitionFrameRateLimit` was observed at **3.3323 fps**
against a 300 ms exposure — the cleanest place to test the policy.

- [ ] 901 ms: warning appears, **ARM greys out**, tooltip names 900 ms
- [ ] Re-check *Keep current*: ARM available again
- [ ] 899 ms, ARM: does 1 Hz hold?

RESULT at 899 ms — worker FPS `________` · duplicates seen? `____` ·
`AcquisitionFrameRateLimit` now reads `________`

> The camera's own limit at 899 ms should be ≈1.11 fps, still above the
> 1 Hz trigger. If duplicates appear anyway, transport overhead is larger
> than the 10% reserve assumes and `_MAX_EXPOSURE_PERIOD_FRACTION` needs
> lowering. This number is the whole reason the ceiling is documented as a
> policy rather than a fact.

## 7. Read-access refusal

kSA is off on this chamber, so force the condition deliberately:

- [ ] Open the Vimba X Viewer (it takes Full access)
- [ ] Uncheck Keep current, choose 100 ms, ARM
- [ ] The GUI **refuses** with "Manual exposure requires Full camera
      access" — it must not connect in Read mode and report an exposure it
      did not set
- [ ] Close the Viewer, re-ARM: succeeds

## 8. Failure restores the original — CONDITIONAL, not a merge blocker

Only runnable if §1's `ExposureTimeAbs` range excludes part of the 1–999 ms
span the GUI can produce. The k700-12 spec puts the Manta at
0.026–60,000 ms, so in all likelihood every selectable value is valid and
no out-of-range request can be made through the UI.

Device range from §1: `________` – `________` us
Reachable through the GUI? `____`

If reachable:

- [ ] Request the out-of-range value: the error names the camera range
- [ ] Re-probe after the failure

RESULT: `________` us — back to the §1 baseline? `____`

If not reachable, mark N/A —
`test_out_of_range_exposure_fails_without_writing` and
`test_bad_readback_restores_the_original_exposure` drive these branches
directly. Do not hold the merge on an unreachable step.

## 9. Disarm / re-arm

- [ ] DISARM, change exposure, re-ARM: new value confirmed
- [ ] After DISARM, *Keep current* checkbox state and control enablement
      agree
- [ ] No stale readback from the previous arm cycle

## 10. Session metadata

Run ~2 minutes at a known exposure, then inspect `session_metadata.json`:

- [ ] `camera_exposure_requested_ms` and `camera_exposure_readback_ms`
- [ ] `camera_sensor_settings_at_connect` with `read_at_utc`,
      `exposure_us_feature` = `ExposureTimeAbs`, `black_level`,
      `device_serial` = `50-0503464907`
- [ ] `chamber_id` = `chmbe`
- [ ] Repeat, ending by **closing the window** rather than STOP — same keys

## 11. Provenance integrity

```powershell
python scripts\validate_temporal_session.py <session_dir>
```

- [ ] `capture_sequence` strictly increasing across the session
- [ ] `heartbeat_log.csv` carries `captured_at_utc` and `capture_sequence`
- [ ] No runs of identical frames

RESULT: `________`

> This session is also the first candidate input for Yao's offline labeler
> (`tools/rheed_postprocessing_labeling`). If §11 passes, zip the session
> directory and try a report build — it exercises the provenance contract
> end to end and is the cheapest possible check that the archive is
> actually loadable.

## 12. Pyrometer regression check

Earlier drafts of this document called the RTS polarity "unresolved". That
was wrong: `60ce781` reversed it and `45a98af` put it back, so this branch
and `main` **agree** at `pyrometer_rts=False`, hardware-verified 2026-08-05.

The check is still worth running, for a different reason. This branch
carries Yao's `pyrometer_modbus_backend="raw_serial"` for Ch-MBE, where
`main` uses the default `pymodbus`. That is a different transport reading
the same probe, it has not been exercised alongside the exposure work, and
`drivers/config.py` still conflicts textually with `main`. Confirm the
pyrometer reads here before trusting any session recorded on this branch.

- [ ] Pyrometer connects on COM3 at 115200 via the `raw_serial` backend
- [ ] Live temperature is plausible and non-zero
- [ ] `heartbeat_log.csv` `pyrometer_temp_C` is populated

RESULT: `________` °C

---

## Sign-off

| | |
|---|---|
| All steps pass | `____` |
| §6 ceiling number | `________` |
| §12 pyrometer reads | `____` |
| Blocking issues | `________________________` |
| Operator / date | `________________________` |

Do not merge until §3 and §7 pass. Ch-MBE writes to the camera by
default, so the guarantee that matters most is that it stops when told to.
§8 is conditional on the device range and is not a blocker; its logic is
covered by unit tests.
