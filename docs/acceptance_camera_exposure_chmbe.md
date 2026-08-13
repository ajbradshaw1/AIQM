# Ch-MBE camera-exposure acceptance — `codex/gui-brightness-robust-four-output-shadow`

Hardware acceptance for the grower exposure control on the **Chalcogenide
MBE / Omicron PC**. Companion to `acceptance_camera_exposure_ombe.md`.

**Do not substitute one document for the other.** The chambers differ in the
one way that matters most here:

| | Ch-MBE (Omicron) | O-MBE (Bulbasaur) |
|---|---|---|
| `camera_exposure_us` | **`300_000.0`** | `500_000.0` |
| Control opens on | **300 ms — ARM writes** | 500 ms — ARM writes |
| Measured on that chamber? | **Yes, 2026-08-06** | No — pending |
| kSA | off (2026-08-05 decision) | must be closed before ARM |
| Access mode in practice | Full | Full (required) |

Both chambers now write on ARM. **The values are deliberately different and
must not be copied across.** Ch-MBE's 300 ms is a real measurement on this
camera (Manta G-033B, serial 50-0503464907); O-MBE's 500 ms is a specified
starting point not yet measured on that chamber. A "tidy-up" that unified
them would silently discard a measurement.

So on Ch-MBE the **first ARM writes to the camera**, by design and by config.
That makes §3 and §4 the load-bearing steps here.

Every write is volatile — `UserSetSave` is never called — so a camera
power-cycle restores the stored user set.

Camera identity: Manta G-033B, serial **50-0503464907**, device
**DEV_000F314E8D67**, firmware 00.01.44.18241.

---

## The control, as it actually is

One **spin box** labelled *Direct exposure*, in the Session tab's config panel.

- **Zero displays `Keep current`** and performs no camera write. There is no
  separate checkbox.
- There is **no slider**.
- The maximum is `900 / camera_fps` ms — the 90%-of-trigger-period safety
  policy. At the default 1 Hz that is **900 ms**. Values above it are
  **unrepresentable**: the spin box will not accept them, so there is no
  "ARM is blocked" state to test.
- The control is disabled outside direct-camera mode.

**The config panel is locked while ARMed.** Every value change below follows:

```
DISARM  →  set the control  →  ARM  →  observe  →  DISARM
```

If a control is greyed out unexpectedly, check the ARM state before
concluding the feature is broken.

---

## Setup

The branch lives on the lab remote. Fetch the exact ref rather than
assuming a local `origin` alias points at it:

```powershell
cd C:\Users\Omicron\AIQM-Software-Hardware-Integration
.\.venv\Scripts\Activate.ps1
git fetch https://github.com/AaravSonthalia/AIQM-Software-Hardware-Integration.git codex/gui-brightness-robust-four-output-shadow
git switch --detach FETCH_HEAD
git rev-parse --short HEAD
$env:AIQM_CHAMBER = "chmbe"
$env:AIQM_CHAMBER                # confirm — growth_monitor_chmbe.py uses
                                 # setdefault, so a stale value survives
```

Record: commit `__________` · date `__________` · operator `__________`

Close kSA **and** the Vimba X Viewer before §1–§6. Either one holding the
camera forces Read access, and every writable bit then reads False — which
is indistinguishable from a firmware refusal. §7 re-opens the Viewer on
purpose.

**The GUI now opens on camera mode `vimba`**, not `dummy`. That means the
first ARM reaches the camera and performs the configured exposure write
with no further selection — close kSA and the Vimba X Viewer first.
Selecting `dummy` reports no exposure at all, so the control looks inert
rather than broken.

---

## 1. Baseline — record what the camera already has

A restore target on paper as well as in the driver.

**DISARM the GUI first** (or close it). The probe needs the camera, and an
armed GUI holds it — an armed session will make the probe fail or read
everything as non-writable. This applies to *every* probe run below, not
just this one.

```powershell
python scripts\vimba_feature_probe.py --filter exposure --require-id DEV_000F314E8D67 --output D:\AIQM-evidence\chmbe_exposure_baseline.json
```

The `--require-id` gate is not decoration: `(E0022060)` appears in the model
string on **both** chambers' cameras, so it is a kSA/model config code, not
a unit serial. The `DEV_*` ID is what actually distinguishes this camera
from Bulbasaur's. If the gate rejects, you are not on the camera you think.

RESULT — device ID confirmed? `____` · serial `50-0503464907`? `____`

RESULT — `ExposureTimeAbs` = `________` us · `ExposureAuto` = `________` ·
device range = `________`–`________` us · increment = `________`

> This probe has never completed a full sweep on hardware — the 2026-08-06
> run died on its first feature and the fix has only been verified against
> mocks. If it crashes, capture the traceback; a partial
> `report["describe_errors"]` is still written.

## 2. The control opens at 300 ms

Launch the GUI. Before touching anything:

```powershell
python growth_monitor_chmbe.py
```

- [ ] *Direct exposure* reads **300 ms**, not `Keep current`
- [ ] Camera mode is `vimba`
- [ ] Switching camera mode to `screengrab` greys the control out; switching
      back to `vimba` re-enables it

RESULT — opening value: `________` ms

If this reads `Keep current`, the chamber config did not take. Stop and
re-check `$env:AIQM_CHAMBER`.

## 3. The default write applies and is confirmed — **WRITES**

Press **ARM**.

- [ ] Live frames appear
- [ ] Status bar reads "Direct camera exposure confirmed: 300 ms"
- [ ] The confirmed number is the **readback** — what the camera reports
      after the write, not what was asked for. The request is snapped
      onto the device's own increment grid first, so the two must now
      MATCH. A device that lands elsewhere is refused and the original
      exposure is restored, so a mismatch here is a failure, not rounding
- [ ] **DISARM**

RESULT — status bar: `________________________________`

Re-run §1's probe and confirm the camera now reads what the GUI claimed:

RESULT — `ExposureTimeAbs` after ARM: `________` us

## 4. Other values apply and are visibly correct — **WRITES**

For each row: `DISARM → set value → ARM → observe → DISARM`.

| Requested | Status bar shows | Image vs previous | Probe readback |
|---|---|---|---|
| 50 ms | `________` | `________` | `________` |
| 150 ms | `________` | `________` | `________` |
| 600 ms | `________` | `________` | `________` |

- [ ] Brightness increases monotonically with exposure

## 5. `Keep current` writes nothing — but still *reads*

This is the step most likely to be misread, so be precise about what
"no write" means. On the `Keep current` path the driver still reads the
camera's existing exposure and reports it. It does **not** set it.

So expect:

- [ ] DISARM, set *Direct exposure* to **0** — it displays `Keep current`
- [ ] Note the camera's exposure from §4's last row
- [ ] ARM, confirm frames flow
- [ ] The status bar **does** show an exposure message, naming the value the
      camera already had — this is the *observed* exposure, not a
      confirmation that anything was written
- [ ] DISARM, then re-run §1's probe
- [ ] `ExposureTimeAbs` is **unchanged** from §4's last row

RESULT — status bar value: `________` ms
RESULT — probe: `________` us — unchanged? `____`

> A message reading "exposure confirmed" here is **not** a failure. The
> wording is imprecise on the `Keep current` path — it reports what was
> observed. The gate is the probe: the camera's value must not have moved.
> The only real failure is `ExposureTimeAbs` changing.

## 6. The ceiling is unrepresentable

- [ ] DISARM, try to type **901** into the spin box
- [ ] It clamps to **900** — the value cannot be entered
- [ ] Confirm the tooltip names the 900 ms ceiling and the 1 Hz loop

RESULT — maximum accepted: `________` ms

> The 900 ms figure is our 90%-of-period policy, not a measured Manta limit.
> If you have time, ARM at 900 ms and record whether the acquisition
> sustains 1 Hz. Duplicate frames are no longer the symptom: the driver
> is not over-triggered at this rate, so acquisition shows up as
> SKIPPED deliveries — worker FPS below the trigger rate, the frame
> number advancing more slowly than 1/s — and, if the shortfall persists
> past the starvation deadline, a "camera not delivering" report. If
> that appears well below the ceiling, `_MAX_EXPOSURE_PERIOD_FRACTION`
> should come down.

RESULT at 900 ms — worker FPS `________` · frames advancing? `____`

## 7. Forced Read-access refusal — **the disarm gate**

Ch-MBE runs with kSA off, so the refusal must be provoked deliberately.

- [ ] DISARM. Set *Direct exposure* to **100 ms** (nonzero)
- [ ] Open the **Vimba X Viewer** and let it take the camera
- [ ] Press **ARM**

Expected — the GUI refuses rather than connecting in Read mode and
pretending the exposure was applied:

- [ ] Status bar names the reason, containing "Full camera access"
- [ ] The GUI is **back at idle**: ARM button reads ARM, config panel is
      editable again
- [ ] It did **not** stay armed with a locked panel

RESULT — status bar: `________________________________`
RESULT — state after refusal: `________`

Then confirm the refusal released the other instruments. Check what is
observable from the GUI rather than guessing at thread state:

- [ ] Pyrometer, MISTRAL and EvapControl value displays have stopped
      updating and/or reset to their idle placeholders
- [ ] The config panel is **editable** again — it locks while armed, so an
      unlocked panel is direct evidence the state really returned to idle
- [ ] Re-arming works (§8) — a half-torn-down state would fail to re-arm

## 8. Re-arm after removing the competing owner

- [ ] Close the Vimba X Viewer
- [ ] Press ARM again with the same 100 ms request
- [ ] It succeeds, frames flow, status bar confirms 100 ms
- [ ] DISARM

RESULT: `________`

### 8b. `Keep current` under Read access — OPTIONAL on this chamber

- [ ] Re-open the Viewer, set *Direct exposure* to 0 (`Keep current`), ARM
- [ ] The connect is **not refused** — no write was requested
- [ ] Frames flow

RESULT — connect refused? `____` · frames flowing? `____`

> **Frames may not flow here, and that is not a defect on Ch-MBE.** Read
> access only streams if the camera's persistent user set has multicast /
> read sharing enabled — the Task #187 configuration, which was validated
> on Bulbasaur and has **never been validated on Ch-MBE** (question #8 in
> the 2026-08-05 parity audit was descoped when this chamber went
> Vimba-direct with kSA off).
>
> What this step gates is only that `Keep current` is **not refused** under
> Read access. Whether frames then arrive is a multicast configuration
> question, not an exposure question. Record it and move on.

- [ ] DISARM, close the Viewer

## 9. A short session records it honestly

Run a ~2-minute session at a chosen exposure, then open
`session_metadata.json`:

- [ ] `camera_exposure_requested_ms` — what was asked for
- [ ] `camera_exposure_readback_ms` — what the camera confirmed
- [ ] Repeat ending the session by **closing the window** instead of STOP —
      both keys must still be present

RESULT — requested `________` ms · readback `________` ms

> This branch records **only** these two keys. The broader
> `camera_sensor_settings_at_connect` snapshot (gain, black level, device
> serial, timestamp) is deliberately **not** part of this release.

Now a `Keep current` session. The two keys behave **differently**, which is
the point:

- [ ] `camera_exposure_requested_ms` is **`null`** — nothing was requested
- [ ] `camera_exposure_readback_ms` is **numeric** — the driver still read
      the camera's existing exposure

RESULT — requested `________` · readback `________` ms

That asymmetry is the archive's record that the session ran at whatever the
camera was already set to, and that the GUI did not choose it. Both keys
`null` would mean the readback never happened; both numeric would mean a
write was requested.

## 10. Frame freshness

At 1 Hz against a 300 ms exposure the camera is not over-triggered, so
acquisition should comfortably keep up with the poll loop.

- [ ] Worker FPS tracks the camera, not the poll interval
- [ ] The frame counter advances steadily with no long stalls
- [ ] No starvation report ("camera is connected but not delivering") appears
      while the preview is live

> **Do not eyeball the live view for repeated images.** Genuinely fresh
> frames can be pixel-identical — a static RHEED pattern at a steady exposure
> produces exactly that — so "looks the same" proves nothing either way.
> At this commit the driver can still re-serve a cached frame, so a repeated
> image is not by itself evidence of a fault. What hardware acceptance can
> establish is CADENCE: whether the real camera sustains the expected rate,
> whether the counter keeps advancing, and whether the first-frame starvation
> deadline ever trips.

RESULT — observed FPS: `________`

## 11. No user set was saved — OPTIONAL, ask first

**Do not power-cycle the camera without the chamber owner's approval.** A
GigE power-cycle drops the link, and on Ch-MBE the camera sits on an APIPA
address (`169.254.x.x`) with slow rediscovery. Skip this step rather than
interrupt someone's work; it is not a merge gate.

- [ ] Approval obtained from `________________`
- [ ] Power-cycle the camera
- [ ] Re-run §1's probe

RESULT — `ExposureTimeAbs` after power-cycle: `________` us

Expected: the §1 baseline, because nothing in the driver calls
`UserSetSave`. If instead it reads the last value the GUI wrote, **record
it and investigate** — do not conclude the GUI saved a user set. Other
explanations are at least as likely: the Vimba Viewer may have saved a user
set at some point, the camera may have been configured to auto-load a set
containing that value, or the power-cycle may not have fully dropped
power. Confirm against the driver's actual behaviour before treating it as
a defect in this branch.

---

## Sign-off

| | |
|---|---|
| All steps pass | `____` |
| §6 ceiling number | `________` |
| Blocking issues | `________________________` |
| Operator / date | `________________________` |

**Merge gates: §2, §3, §5, §7, §8, §9.**

§2 confirms the chamber config took at all. §3 and §5 are the positive and
negative write paths — evidence that an exposure is actually applied and
confirmed by readback, and that `Keep current` leaves the camera's value
untouched. §7 and §8 are the refusal and the recovery from it. §9 is the
archive record, including the requested-`null` / readback-numeric
asymmetry.

**Not gates:** §6 and §10 are measurements to record. §8b is optional and
depends on unvalidated Ch-MBE multicast. §11 is optional and needs the
chamber owner's approval.
