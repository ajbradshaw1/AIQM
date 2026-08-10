# O-MBE camera-exposure acceptance — `feat/ombe-exposure-integration`

Hardware acceptance for the grower exposure control and the capture
provenance that ships with it. Run on **Bulbasaur** before the PR merges to
the fork's `main`.

Companion: `acceptance_camera_exposure_chmbe.md`. The two chambers are
**not** interchangeable — O-MBE defaults to *Keep current* and normally
shares the camera with kSA; Ch-MBE defaults to writing 300 ms with kSA off.
Run the one for the machine you are sitting at.

Every step is read-only unless marked **WRITES**. The writes are volatile:
`UserSetSave` is never called, so a camera power-cycle restores the
grower's stored user set.

---

## What is being accepted

| Behaviour | Where it comes from |
|---|---|
| *Keep current* issues no exposure write | `VmbCamera._configure_exposure` early return |
| A requested exposure is applied and **confirmed by readback** | same, plus `exposure_us` property |
| Write requires Full access and `ExposureAuto=Off` | same, fail-closed |
| Out-of-range / bad-readback restores the original | same, `except` block |
| Above the trigger-rate ceiling, ARM is disabled | `GrowthMonitor.exposure_blocks_arm` |
| Session metadata records requested + confirmed + connect snapshot | `_session_metadata_with_camera` |
| Re-served frames are marked, not counted | `CameraState.is_duplicate` |

The 900 ms ceiling at 1 Hz is **our 90%-of-period safety policy**, not a
measured Manta limit. Step 6 is the first time it is probed on hardware.

---

## Setup

```powershell
conda activate ai4mbe-gui
cd C:\path\to\AIQM-Software-Hardware-Integration
git fetch origin
git checkout feat/ombe-exposure-integration
git rev-parse --short HEAD      # expect 18b8894 or later
$env:AIQM_CHAMBER = "ombe"
```

Record: commit `__________`  ·  date `__________`  ·  operator `__________`

O-MBE config ships `camera_exposure_us = None`, so **the control opens on
*Keep current*** and the first arm must not touch the camera.

---

## 1. Baseline — write down what the camera already has

Before anything else, so a restore target exists on paper as well as in the
driver.

```powershell
python scripts\vimba_feature_probe.py --filter exposure --output D:\AIQM-evidence\ombe_exposure_baseline.json
```

Close the Vimba X Viewer first — it holds Full access and every feature
will read `writable=False`, which is indistinguishable from a firmware
refusal.

RESULT — `ExposureTimeAbs` = `________` us · `ExposureAuto` = `________` ·
`BlackLevel` = `________` · `GevTimestampTickFrequency` = `________`

> This probe has never completed a full sweep on hardware — the 2026-08-06
> Ch-MBE run died on its first feature and the fix (`e086a79`) has only
> been verified against mocks. If it crashes, capture the traceback; a
> partial `report["describe_errors"]` is still written.

## 2. *Keep current* writes nothing — **the most important step**

Launch the GUI, leave **Keep current** checked, press **ARM**.

- [ ] Slider and textbox are greyed out while Keep current is checked
- [ ] ARM succeeds, live frames appear
- [ ] Re-run step 1's probe: `ExposureTimeAbs` is **unchanged** from §1

RESULT: `________` us — matches §1? `____`

If this fails, stop. Everything else is secondary to the GUI not silently
reconfiguring a grower's camera.

## 3. Applied exposure is visibly correct — **WRITES**

Uncheck *Keep current*. For each value: set it, ARM, observe, DISARM.

| Requested | Status bar shows | Image vs previous | Probe readback |
|---|---|---|---|
| 50 ms | `________` | `________` | `________` |
| 150 ms | `________` | `________` | `________` |
| 300 ms | `________` | `________` | `________` |

- [ ] Brightness increases monotonically with exposure
- [ ] Status bar reads "Direct camera exposure confirmed: N ms"
- [ ] The confirmed value is the **readback**, which may differ from the
      request if the device quantises to its increment grid — a small
      difference is correct behaviour, not a failure

## 4. Slider and textbox are one value

- [ ] Dragging the slider updates the textbox live
- [ ] Typing in the textbox moves the slider
- [ ] Range is 1–999 ms at both ends

## 5. Non-direct modes disable the control

- [ ] Switch camera mode to `screengrab` → slider, textbox and Keep current
      all grey out
- [ ] Switch back to `vimba` → they re-enable

## 6. The ceiling — first hardware probe of the 90% policy

- [ ] Set 901 ms: inline warning appears, **ARM greys out**
- [ ] ARM tooltip names the 900 ms limit
- [ ] Re-check *Keep current*: ARM becomes available again
- [ ] Set 899 ms and ARM: does the camera actually sustain 1 Hz?

RESULT at 899 ms — worker FPS `________` · duplicate frames seen? `____`

> This is the evidence the policy needs. If 899 ms sustains 1 Hz cleanly,
> the 90% figure is defensible. If duplicates appear well below it, the
> ceiling is too generous and `_MAX_EXPOSURE_PERIOD_FRACTION` should come
> down. Record the number either way.

## 7. Read-access refusal — **the kSA coexistence case**

This is the O-MBE-specific one: growers normally have kSA open.

- [ ] Open kSA and let it take the camera
- [ ] Uncheck Keep current, choose 100 ms, press ARM
- [ ] The GUI **refuses** with "Manual exposure requires Full camera
      access", rather than connecting in Read mode and pretending
- [ ] With *Keep current* checked, ARM in Read mode still succeeds and
      frames flow

## 8. Failure is legible

- [ ] Enter a value the camera rejects (try 1 ms if below the device
      minimum): the error names the camera range
- [ ] After any failed ARM, re-probe: exposure is back at its **original**
      value, not the rejected one

RESULT after failure: `________` us — matches §1? `____`

## 9. Disarm / re-arm is clean

- [ ] DISARM, change exposure, re-ARM: new value confirmed in the status bar
- [ ] After DISARM, *Keep current* state and control enablement agree —
      checked means greyed out, unchecked means editable
- [ ] Stale readback from the previous cycle is not reported

## 10. A short session records it honestly

Run a ~2-minute session with a chosen exposure, then open
`session_metadata.json`:

- [ ] `camera_exposure_requested_ms` — what was asked for
- [ ] `camera_exposure_readback_ms` — what the camera confirmed
- [ ] `camera_sensor_settings_at_connect` present, with `read_at_utc`,
      `exposure_us`, `exposure_us_feature` (expect `ExposureTimeAbs`),
      `gain`, `black_level`, `device_serial`
- [ ] Repeat ending the session by **closing the window** instead of STOP —
      the same keys must be present. Both paths were wired deliberately;
      this checks it on real hardware.

## 11. Duplicate marking behaves at 1 Hz

At the default 1 Hz poll against a 300 ms exposure the camera is not
over-triggered, so duplicates should be rare or absent.

- [ ] Worker FPS tracks the camera, not the poll interval
- [ ] Heartbeat frame count over a known interval matches expectation, with
      no runs of identical frames in `heartbeat_log.csv`
- [ ] `capture_sequence` is strictly increasing across the whole session

```powershell
python scripts\validate_temporal_session.py <session_dir>
```

RESULT: `________`

---

## Sign-off

| | |
|---|---|
| All steps pass | `____` |
| §6 ceiling number | `________` |
| Blocking issues | `________________________` |
| Operator / date | `________________________` |

Do not merge the PR until §2, §7 and §8 pass — those are the three that
protect a grower's camera from the GUI.
