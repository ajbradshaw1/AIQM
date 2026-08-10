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

## State discipline — read before starting

**The config panel is locked while ARMed.** `_apply_state` disables every
config widget on entering the armed state, so exposure cannot be changed
without disarming first. Every value change in this document therefore
follows:

```
DISARM  →  set the control  →  ARM  →  observe  →  DISARM
```

If a control is greyed out and you did not expect it, check the ARM state
before concluding the feature is broken. Sections below assume you arrive
**disarmed** unless they say otherwise.

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
- [ ] **DISARM** before continuing

RESULT: `________` us — matches §1? `____`

If this fails, stop. Everything else is secondary to the GUI not silently
reconfiguring a grower's camera.

## 3. Applied exposure is visibly correct — **WRITES**

**DISARM first, then close kSA.** Everything from here to §6 needs Full
access. With kSA holding the camera the driver lands in Read mode and
correctly *refuses* every write in this section — which looks like a
failure of the exposure feature rather than the coexistence rule working
as designed. (Provoking that refusal deliberately is §7's job.)

- [ ] DISARMed
- [ ] kSA closed
- [ ] Vimba X Viewer closed

Uncheck *Keep current*. For each row:
`DISARM → set value → ARM → observe → DISARM`.

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

Arrive **disarmed** — the slider must be editable to reach 901 ms.

- [ ] Set 901 ms: inline warning appears, **ARM greys out**
- [ ] ARM tooltip names the 900 ms limit
- [ ] Re-check *Keep current*: ARM becomes available again
- [ ] **Uncheck *Keep current* again** — the previous check left it on, and
      899 ms cannot be requested while it is checked
- [ ] Set 899 ms, ARM: does the camera actually sustain 1 Hz?
- [ ] **DISARM** before continuing

RESULT at 899 ms — worker FPS `________` · duplicate frames seen? `____`

> This is the evidence the policy needs. If 899 ms sustains 1 Hz cleanly,
> the 90% figure is defensible. If duplicates appear well below it, the
> ceiling is too generous and `_MAX_EXPOSURE_PERIOD_FRACTION` should come
> down. Record the number either way.

## 7. Read-access refusal — **the kSA coexistence case**

This is the O-MBE-specific one: growers normally have kSA open. Arrive
**disarmed** — the exposure controls are gated by ARM state, camera mode
and *Keep current*, so the value has to be set before arming. Setting it
before opening kSA is not required by the GUI, but it keeps the refusal the
only variable under test.

> **The GUI stays ARMED after a refused connect.** `_on_arm` sets the state
> to `armed` before the camera thread reports back, and the async error path
> stops automation without returning to `idle` — the only `set_state("idle")`
> is in the DISARM handler. So after the expected refusal the config panel is
> still locked and the ARM button now reads DISARM. Press it once before
> continuing. This is pre-existing behaviour, not something this branch
> introduced; see the follow-up note at the end of this document.

- [ ] Uncheck *Keep current*, set 100 ms (while disarmed)
- [ ] Open kSA and let it take the camera
- [ ] Press ARM
- [ ] The GUI **refuses** with "Manual exposure requires Full camera
      access", rather than connecting in Read mode and pretending
- [ ] The GUI is still **armed** — the button reads DISARM. Press **DISARM**
- [ ] Check *Keep current*, then ARM: Read mode succeeds and frames flow
- [ ] **DISARM**, close kSA

## 8. Failure is legible — CONDITIONAL, not a merge blocker

Only runnable if §1's `ExposureTimeAbs` range has a **minimum above 1 ms**
or a **maximum below 999 ms**. The k700-12 spec puts the Manta's range at
0.026–60,000 ms, so every value the 1–999 ms GUI can produce is very
likely in range and there is no way to inject an out-of-range request
through the UI at all.

Device range from §1: `________` – `________` us
Reachable through the GUI? `____`

If reachable:

- [ ] Request the out-of-range value: the error names the camera range
- [ ] Re-probe: exposure is back at its **original** value

RESULT after failure: `________` us — matches §1? `____`

If not reachable, mark N/A. The restoration and range-rejection paths are
covered by `test_out_of_range_exposure_fails_without_writing` and
`test_bad_readback_restores_the_original_exposure` in
`tests/test_vimba_camera.py`, which drive those branches directly. Do not
hold the merge on a step the hardware makes unreachable.

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

**Merge gates: §2, §3, §7 — and §10 if this is the chamber used for final
acceptance.**

An earlier revision gated only on §2 and §7, which is to say only on the
feature declining to act. That would have let the branch merge without any
evidence that a new exposure is ever successfully applied, confirmed by
readback, or visible in the image. §3 is the positive path and is now
required alongside the refusals.

§10 is a gate on the acceptance chamber because this branch also changes
what lands in the archive.

§8 stays conditional on the device range and is not a blocker — its logic
is covered by unit tests.

## Follow-up noticed while writing this

A refused camera connect leaves the GUI in the `armed` state. `_on_arm`
sets `armed` before the camera thread answers, and the async error branch
in `_on_camera_state` stops the heartbeat, auto-capture and classifier but
never calls `set_state("idle")`. The grower is left with a locked config
panel and an ARM button reading DISARM, after an arm that did not succeed.

Pre-existing, not introduced by this branch, and deliberately not fixed
here — a state-machine change wants its own review rather than riding along
with an exposure feature. Worth raising as a separate issue.
