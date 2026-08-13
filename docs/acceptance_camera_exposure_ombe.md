# O-MBE camera-exposure acceptance — `codex/gui-brightness-robust-four-output-shadow`

Hardware acceptance for the grower exposure control on **Bulbasaur**.
Companion to `acceptance_camera_exposure_chmbe.md`.

**Do not substitute one document for the other.** The chambers differ in the
one way that matters most here:

| | O-MBE (Bulbasaur) | Ch-MBE (Omicron) |
|---|---|---|
| `camera_exposure_us` | **`500_000.0`** | `300_000.0` |
| Control opens on | **500 ms — ARM writes** | 300 ms — ARM writes |
| Value measured on this chamber? | **No — pending this run** | Yes, 2026-08-06 |
| kSA | **must be CLOSED before ARM** | off |
| Access mode in practice | **Full (required)** | Full |

> ### ⚠ This is a workflow change for O-MBE growers
>
> O-MBE previously shipped `camera_exposure_us = None`: the control opened on
> `Keep current`, ARM performed **no** camera write, and the GUI therefore
> coexisted with kSA holding the camera in Read mode. That was the normal
> path on this chamber.
>
> It now requests **500 ms** and **fails ARM closed** if the write cannot be
> proven applied. A write needs **Full** camera access, so **kSA and the
> Vimba X Viewer must be closed before ARM.** A grower who leaves kSA open
> will see ARM refused — correctly, but it is new behaviour and they have not
> been trained on it.
>
> `Keep current` is still selectable (set the spin box to 0) and still
> coexists with kSA — it is now an explicit opt-out rather than the default.
> §7 tests that escape hatch.
>
> The two chamber values are **deliberately different and must not be copied
> across.** 300 ms is a measurement taken on the Ch-MBE Manta G-033B (serial
> 50-0503464907). 500 ms is O-MBE's specified starting point and is **not yet
> measured on this camera** — recording its confirmed readback is part of
> what this run is for.

Because ARM now writes by default, §3 (the write is visibly correct) and §6
(the kSA-open refusal) are the load-bearing steps on this chamber. §6 in
particular is no longer a provoked edge case — it is what a grower hits on
their first attempt if they forget to close kSA.

Every write is volatile — `UserSetSave` is never called — so a camera
power-cycle restores the stored user set.

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

---

## Setup

The branch lives on the lab remote. Fetch the exact ref rather than
assuming a local `origin` alias points at it:

```powershell
cd C:\path\to\AIQM-Software-Hardware-Integration
git fetch https://github.com/AaravSonthalia/AIQM-Software-Hardware-Integration.git codex/gui-brightness-robust-four-output-shadow
git switch --detach FETCH_HEAD
git rev-parse --short HEAD
$env:AIQM_CHAMBER = "ombe"
```

Record: commit `__________` · date `__________` · operator `__________`

**The GUI now opens on camera mode `vimba`**, not `dummy`. That means the
first ARM reaches the camera and performs the configured exposure write
with no further selection — close kSA and the Vimba X Viewer first.
Selecting `dummy` reports no exposure at all, so the control looks inert
rather than broken.

---

## 1. Baseline — record what the camera already has

Close the Vimba X Viewer first — it holds Full access and every feature will
read `writable=False`, which is indistinguishable from a firmware refusal.

**DISARM the GUI first** (or close it). The probe needs the camera, and an
armed GUI holds it. This applies to *every* probe run below.

```powershell
python scripts\vimba_feature_probe.py --filter exposure --require-id DEV_000F314F7A86 --output D:\AIQM-evidence\ombe_exposure_baseline.json
```

The `--require-id` gate is not decoration: `(E0022060)` appears in the model
string on **both** chambers' cameras, so it is a kSA/model config code, not
a unit serial. The `DEV_*` ID is what distinguishes Bulbasaur's camera from
Ch-MBE's. If the gate rejects, you are not on the camera you think.

RESULT — device ID confirmed? `____`

RESULT — `ExposureTimeAbs` = `________` us · `ExposureAuto` = `________` ·
device range = `________`–`________` us · increment = `________`

> This probe has never completed a full sweep on hardware. If it crashes,
> capture the traceback; a partial `report["describe_errors"]` is still
> written.

## 2. The control opens at 500 ms and the first ARM writes it

O-MBE ships `camera_exposure_us = 500_000.0`, so the control must open on
500 ms and the first arm must apply it. **Close kSA and the Vimba X Viewer
first** — this step needs Full access, and §6 is where the kSA-open refusal
is tested deliberately.

```powershell
python growth_monitor_app.py
```

- [ ] kSA closed · Vimba X Viewer closed
- [ ] *Direct exposure* opens reading **500 ms**, not `Keep current`
- [ ] Confirm camera mode reads **`vimba`** (now the default)
- [ ] Press **ARM** — frames appear, ARM is not refused
- [ ] Status bar reads "Direct camera exposure confirmed: 500 ms"
- [ ] **DISARM**, then re-run §1's probe
- [ ] `ExposureTimeAbs` now reads **500000** us

RESULT — opening value: `________` ms · status bar: `________` ms ·
probe after ARM: `________` us

> **The confirmed number is the READBACK, not the request.** The request is
> snapped onto the device's own increment grid before the write, so the
> readback must MATCH it — a device that lands elsewhere is refused and
> the original exposure restored. **Record the readback** — 500 ms has not
> been measured on this camera before, and this line is the measurement.

**If ARM is refused here, check kSA is actually closed before treating it as
a defect.** A refusal with kSA open is §6 working, not §2 failing.

## 2a. `Keep current` still writes nothing — the opt-out

The zero value remains available and must still perform no write. This was
the O-MBE default before the 500 ms change; it is now an explicit choice.

- [ ] DISARM, set *Direct exposure* to **0** (`Keep current`)
- [ ] ARM — frames appear
- [ ] DISARM, re-run the probe
- [ ] `ExposureTimeAbs` is **unchanged from §2's 500000**, not restored to §1

RESULT — probe: `________` us

> **Expect a status-bar exposure message anyway.** On the `Keep current`
> path the driver still *reads* the camera's exposure and reports it; it
> just does not *set* it. A message naming the value the camera already had
> is correct behaviour, not a silent write. The gate is the probe — the
> camera's value must not have moved.

**If the probe moves on this step, stop.** Everything else is secondary to
the GUI not silently reconfiguring a grower's camera when it was told not to.

## 3. Applied exposure is visibly correct — **WRITES**

**DISARM first, then close kSA.** Everything from here to §5 needs Full
access. With kSA holding the camera the driver lands in Read mode and
correctly *refuses* every write in this section — which looks like a broken
feature rather than the coexistence rule working. Provoking that refusal
deliberately is §6's job.

- [ ] DISARMed · kSA closed · Vimba X Viewer closed

For each row: `DISARM → set value → ARM → observe → DISARM`.

| Requested | Status bar shows | Image vs previous | Probe readback |
|---|---|---|---|
| 50 ms | `________` | `________` | `________` |
| 150 ms | `________` | `________` | `________` |
| 300 ms | `________` | `________` | `________` |
| 500 ms (the O-MBE default) | `________` | `________` | `________` |

- [ ] Brightness increases monotonically with exposure
- [ ] Status bar reads "Direct camera exposure confirmed: N ms"
- [ ] The confirmed number is the **readback**, and it must MATCH the
      request after grid snapping. A mismatch is refused and restored, so
      seeing one here is a failure rather than expected rounding

## 4. Returning to `Keep current` stops writing

- [ ] DISARM, set *Direct exposure* back to **0** (`Keep current`)
- [ ] ARM — the status bar reports the exposure the camera currently has,
      which is §3's last value. That is a read, not a write
- [ ] DISARM, then probe: exposure still reads §3's last value, **not**
      §1's baseline — the previous write was volatile but real, and nothing
      restores it until a power-cycle
- [ ] The value did not move as a result of arming on `Keep current`

RESULT — status bar: `________` ms · probe: `________` us

## 5. The ceiling is unrepresentable

- [ ] DISARM, try to type **901** into the spin box
- [ ] It clamps to **900** — the value cannot be entered
- [ ] Tooltip names the 900 ms ceiling and the 1 Hz loop

RESULT — maximum accepted: `________` ms

> The 900 ms figure is our 90%-of-period policy, not a measured Manta limit.
> If time allows, ARM at 900 ms and record whether acquisition sustains 1 Hz.

RESULT at 900 ms — worker FPS `________` · frames advancing? `____`

## 6. kSA-open refusal — **the operational case**

This is the O-MBE-specific step and the normal grower condition: kSA is
open and sharing the camera.

- [ ] DISARM. Set *Direct exposure* to **100 ms** (nonzero)
- [ ] Open **kSA** and let its Live Video take the camera
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

## 7. `Keep current` coexists with kSA — the opt-out path

With kSA still open:

- [ ] Set *Direct exposure* to **0** (`Keep current`)
- [ ] ARM — **not refused**, because no write was requested
- [ ] Read mode is negotiated and frames flow
- [ ] The status bar reports the camera's current exposure — the read
      happens in Read mode too
- [ ] DISARM

RESULT — refused? `____` · frames flowing? `____` · reported `________` ms

This **used to be** the default O-MBE workflow. Since O-MBE now ships a
500 ms default it is an explicit opt-out, but it must keep working: it is
the only way to run the Growth Monitor without closing kSA, and it is what a
grower will fall back to when they need both tools at once.

> **Prerequisite:** frames flow under Read access only if the camera's
> persistent user set has multicast / read sharing enabled — the Task #187
> configuration, validated on Bulbasaur 2026-07-27. If frames do not
> arrive, check that first before treating it as an exposure defect: the
> refusal behaviour and the multicast configuration are independent.
>
> The non-refusal is the gate here. Frame flow is a multicast question.

## 8. Re-arm after closing kSA

- [ ] Close kSA
- [ ] Set *Direct exposure* to 100 ms, press ARM
- [ ] It succeeds, frames flow, status bar confirms 100 ms
- [ ] DISARM

RESULT: `________`

## 9. A short session records it honestly

Run a ~2-minute session at a chosen exposure, then open
`session_metadata.json`:

- [ ] `camera_exposure_requested_ms` — what was asked for
- [ ] `camera_exposure_readback_ms` — what the camera confirmed
- [ ] Repeat ending the session by **closing the window** instead of STOP —
      both keys must still be present

RESULT — requested `________` ms · readback `________` ms

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

> This branch records **only** these two keys. The broader
> `camera_sensor_settings_at_connect` snapshot (gain, black level, device
> serial, timestamp) is deliberately **not** part of this release.

## 10. Frame freshness

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

Worth checking in **both** access modes on this chamber: in Read mode frames
arrive only when kSA triggers, so a slow or stalled counter there is
expected behaviour rather than a defect.

RESULT — Read mode: `________` · Full mode: `________`

## 11. No user set was saved — OPTIONAL, ask first

**Do not power-cycle the camera without the chamber owner's approval.** It
drops the GigE link and interrupts kSA. Skip this rather than disturb
someone's work; it is not a merge gate.

- [ ] Approval obtained from `________________`
- [ ] Power-cycle the camera
- [ ] Re-run §1's probe

RESULT — `ExposureTimeAbs` after power-cycle: `________` us

Expected: the §1 baseline, because nothing in the driver calls
`UserSetSave`. If instead it reads the last value the GUI wrote, **record
it and investigate** — do not conclude the GUI saved a user set. Other
explanations are at least as likely: the Vimba Viewer may have saved a user
set, the camera may auto-load a set containing that value, or the
power-cycle may not have fully dropped power. Note that Bulbasaur's camera
carries a deliberately configured persistent user set for Task #187
multicast, so this camera is *known* to have a saved user set — it just
should not contain an exposure the GUI wrote.

---

## Sign-off

| | |
|---|---|
| All steps pass | `____` |
| §5 ceiling number | `________` |
| Blocking issues | `________________________` |
| Operator / date | `________________________` |

**Merge gates: §2, §3, §6, §7, §9.**

§2 is the one that matters most on this chamber — the control opens on
`Keep current` and must not touch a grower's camera. §3 is the positive
path: evidence that a new exposure is actually applied and confirmed by
readback, not just that the feature declines to act. §6 and §7 are the two
halves of kSA coexistence — refuse a write, allow a read. §9 is the archive
record.

**Not gates:** §5 and §10 are measurements to record. §11 is optional and
needs the chamber owner's approval.

In §6 and §7, the gate is the **refusal decision** — refuse a write under
Read access, allow a read — not whether frames subsequently flow. Frame
flow under Read access depends on the Task #187 multicast user set, which
is a separate configuration concern.
