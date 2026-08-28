# O-MBE and Ch-MBE GUI and RHEED Point-event Annotation

## English Operator Manual

- Manual version: **v2.1**
- Issued: **2026-08-20**
- Scope: Windows O-MBE and Ch-MBE workstations and the shared offline RHEED post-processing labeler

This Markdown manual is the searchable companion to the English PDF manual. The screenshots below are from the actual applications using generated demonstration inputs. They contain no experimental data and do not define operating permission or setpoints.

| Ch-MBE Growth Monitor | O-MBE Growth Monitor |
| --- | --- |
| ![Ch-MBE Growth Monitor with generated inputs](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/chmbe_growth_monitor_dummy.png) | ![O-MBE Growth Monitor with generated inputs](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/ombe_growth_monitor_dummy.png) |

> The offline labeler reads archived files only. It never controls a camera, heater, power supply, or other instrument.

### Quick entry

| Purpose | Double-click |
| --- | --- |
| Ch-MBE live GUI | `Start Ch-MBE Growth Monitor.cmd` |
| O-MBE live GUI | `Start O-MBE Growth Monitor.cmd` |
| Offline RHEED labeler | `Start RHEED Post-processing Labeler.cmd` |
| Refresh shortcuts | `Install AI4MBE Desktop Shortcuts.cmd` |

## 1. Understand the live and offline paths

### In brief

- Growth Monitor acquires data and creates the immutable session evidence.
- The offline labeler reviews completed sessions as editable point events.
- Event labels record reconstruction appearances/disappearances and pattern-clarity changes.

The live and offline paths share event identities but have different authority. During an experiment, **MARK EVENT** records what happened without interrupting the grower. After the experiment, the labeler shows that same event in the **Unfinished** queue so the grower can move the review point to a better saved frame, add or revise concise labels, select an interval Anchor, identify the reviewer, and explicitly complete the review. Comments are useful but optional.

The current classifier concerns the bare STO surface before growth. Keep these concepts separate:

- **Reconstruction event label:** a reviewer's statement that `1x1`, Twinned `2x1`, `c(6x2)`, RT13, or HTR appeared or disappeared.
- **Pattern-clarity event label:** a reviewer's statement that the visible RHEED pattern became **Good** or **Bad**.
- **Image acquisition quality:** whether the captured image is usable for analysis. It is not a statement about surface or FeSe film quality.
- **Model output:** a prediction displayed for reference. It must not overwrite a human event label.

**Bad pattern clarity does not mean image unusable.** A dim, clipped, missing, or otherwise unanalyzable frame belongs to the separate image-usability record and must not be converted into a Bad clarity label. Good/Bad also does not assert chemical surface quality or FeSe film quality.

## 2. Start the correct chamber application

### In brief

1. Refresh the five desktop shortcuts from the current checkout after cloning or moving it.
2. Use the launcher that matches the physical chamber.
3. Verify the exact title, reader modes, live validity, and log path before ARM.
4. Close normally so CSV and JSONL records finish writing.

Install shortcuts from the current checkout after cloning or moving it. Stop if the physical chamber, launcher, title, or displayed chamber identity disagree.

### Unified Windows launcher and shortcut set

Double-click `Install AI4MBE Desktop Shortcuts.cmd` to create or refresh exactly these five shortcuts:

- **O-MBE Growth Monitor**
- **Ch-MBE Growth Monitor**
- **RHEED Post-processing Labeler**
- **AI4MBE Operator Manual**
- **Uninstall AI4MBE Growth Monitor**

The three application shortcuts and their three root-level `Start *.cmd` troubleshooting wrappers route through the shared `scripts/windows/launch_ai4mbe.ps1` launcher with `-Application ombe`, `-Application chmbe`, or `-Application labeler`. The three application shortcuts use the bundled Yang Lab icon at `assets/ai4mbe_app_icon.ico`; the icon is visual identification only and never proves the chamber, repository, commit, or Python environment. The manual shortcut opens the English PDF, and uninstall removes installed program files and shortcuts while preserving experiment sessions and launcher logs.

The shared launcher discovers a compatible 64-bit `ai4mbe-gui` Python, starts from the resolved repository or installed-program root, sanitizes process-local Python and Qt environment variables, and writes diagnostics under `%LOCALAPPDATA%\AI4MBE\LauncherLogs`. It does not install packages, change persistent environment variables, alter instrument settings, or choose acquisition modes. `AI4MBE_GUI_PYTHON` may identify an already validated interpreter; do not create or change it ad hoc during an operating run.

| Chamber | Launcher | Expected title |
| --- | --- | --- |
| Ch-MBE | `Start Ch-MBE Growth Monitor.cmd` | **Chalcogenide MBE Growth Monitor** |
| O-MBE | `Start O-MBE Growth Monitor.cmd` | **Oxide MBE Growth Monitor** |

For O-MBE and Ch-MBE, the shared launcher forces the requested chamber before Python can cache configuration, verifies the chamber profile, and relies on the application's chamber-specific single-instance mutex. Their configurations are not interchangeable. A runtime or chamber-preflight failure blocks launch. The launcher separately reports optional live-driver imports; an optional-driver warning does not hide the GUI, but it is a stop condition for any production mode that needs the missing driver. The operator may still use an unaffected approved mode under the applicable SOP. Windows Graphics Capture is an optional diagnostic mode and is checked by the GUI when selected.

Before ARM, confirm an advancing RHEED sequence, valid temperature state, other instrument validity, current data ages, and intended log directory under the applicable chamber SOP. Launching the GUI never authorizes a setpoint change.

| Ch-MBE Session tab | O-MBE Session tab |
| --- | --- |
| ![Ch-MBE Session tab with generated inputs](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/chmbe_growth_monitor_session.png) | ![O-MBE Session tab with generated inputs](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/ombe_growth_monitor_session.png) |

These are generated demonstrations, not operating defaults.

## 3. Understand Configuration modes and chamber defaults

### In brief

- The four selectors choose how the GUI reads data; they do not change instrument setpoints.
- Both chamber profiles start with `vimba`, `modbus`, `ads`, and `elog`.
- Every `dummy` mode is generated test data, never an instrument reading.

The selections are frozen when a session is armed.

### Camera mode

| Option | Meaning |
| --- | --- |
| `dummy` | Generated STO 1x1 demonstration image. |
| `dummy_c6x2` | Generated c(6x2) demonstration image. |
| `dummy_tw` | Generated Twinned (2x1) demonstration image. |
| `dummy_rt13_tilted` | Rotated RT13 demonstration image for alignment testing. |
| `screengrab` | Windows Graphics Capture of a detached kSA Live Video window. |
| `screengrab_mss` | Legacy monitor-pixel capture; diagnostic use only because overlap, position, and DPI can affect it. |
| `vimba` | Direct AVT camera acquisition through Vimba; production startup default. |

### Vimba exposure and ARM fail-closed behavior

The **Direct exposure** control applies only to direct `vimba` acquisition. The current O-MBE profile requests 500 ms and the current Ch-MBE profile requests 300 ms during ARM. They are deliberately different software requests, not transferable chamber setpoints or operating authorization. The confirmed camera readback, not the displayed request alone, is the evidence of what was applied. The control is locked while armed or running.

- A nonzero request is a volatile write performed during ARM. It requires Full camera access and `ExposureAuto=Off`. Close kSA or Vimba X Viewer when either application holds the camera and prevents Full access.
- `0` displays **Keep current**. It performs no exposure write, but the driver still reads and records the camera's current exposure when that read is available.
- The UI ceiling preserves at least 10 percent of the trigger period for acquisition overhead. The driver also rejects non-finite, out-of-range, or unsafe exposure and trigger combinations before writing.
- After a write, the driver reads the feature back. ARM succeeds only when the requested value can be proven applied. On a post-write failure, the driver attempts a verified restoration of the original exposure and reports the failure; an unverified restoration remains an explicit stop condition.
- The application never calls `UserSetSave`. The change is volatile, and a camera power cycle restores the stored camera user set.

Session metadata distinguishes `camera_exposure_requested_ms` from `camera_exposure_readback_ms`. **Keep current** therefore records a null request and may still record a numeric readback. A requested value without a matching confirmed readback is not evidence that the camera accepted the setting.

### Camera ownership, freshness, and start gate

ARM is one cancellable startup transaction. A failed or abandoned camera setup releases its worker and camera ownership before the GUI returns to idle; do not launch a second process to work around an ARM failure. The direct Vimba path does not re-serve its cached last frame as a new acquisition. Capture sequence and arrival time must advance, and START remains disabled until the current ARM cycle has delivered a qualifying live frame. If delivery stalls, DISARM and investigate camera access, triggering, and the recorded error rather than treating the last displayed image as current data.

Windows Graphics Capture can capture a detached kSA Live Video window while another window covers it, but it fails closed when the source window is minimized, closed, or stops delivering new frames. `screengrab_mss` remains a monitor-pixel diagnostic fallback and can be contaminated by overlap, position, scaling, and display changes.

### Pyrometer mode

| Option | Meaning |
| --- | --- |
| `dummy` | Generated temperature values. |
| `exactus` | Direct serial Exactus protocol. |
| `modbus` | Direct read using the chamber COM, baud, RTS, device ID, and backend; production startup default. |
| `screengrab` | TemperaSure value through Windows UI automation. |

### MISTRAL mode

| Option | Meaning |
| --- | --- |
| `dummy` | Generated voltage and current values. |
| `screengrab` | MistralGui screenshot plus OCR. |
| `jsonrpc` | Experimental HTTP route; it may connect while returning no values. |
| `ads` | Read-only Beckhoff TwinCAT ADS using the chamber profile; production startup default. |

### EvapControl mode

| Option | Meaning |
| --- | --- |
| `dummy` | Generated pressure values. |
| `elog` | Direct read of the current EvapControl `.elo` log; production startup default. |
| `screengrab` | EvapControl window screenshot plus OCR. |

Direct reads avoid OCR but are still independent software workers. A sensor-log row is a latest-value snapshot, not proof that camera exposure, temperature, ADS, and EvapControl were physically sampled at one instant.

<a id="chmbe-approved-defaults"></a>
### Ch-MBE GUI startup defaults

| Selector | Startup selection |
| --- | --- |
| Camera | `vimba` - direct AVT camera read |
| Pyrometer | `modbus` - COM3, 115200 baud, device 1, RTS off, `raw_serial` backend |
| MISTRAL | `ads` - chamber-specific read-only profile, 7 cells |
| EvapControl | `elog` - Ch-MBE `.elo` log directory |

<a id="ombe-approved-defaults"></a>
### O-MBE GUI startup defaults

| Selector | Startup selection |
| --- | --- |
| Camera | `vimba` - direct AVT camera read |
| Pyrometer | `modbus` - COM4, 115200 baud, device 1, RTS off, `pymodbus` backend |
| MISTRAL | `ads` - chamber-specific read-only profile, 6 cells |
| EvapControl | `elog` - direct current `.elo` log read |

### What remains chamber-owner controlled

ROI and crop, classifier package and enable state, save-root approval and naming, intervals, and every instrument setpoint remain governed by the matching chamber SOP. The initial Windows save field uses separate `OMBE` and `ChMBE` folders and remains editable before ARM. Each production profile passes its own explicit directory to `ElogReader`; a generic `AIQM_EVAP_LOG_DIR` is only a standalone-reader fallback and does not override the GUI profile. Session metadata records the effective pyrometer serial settings, configured Elog directory, and exact `.elo` source once connected. Variables absent from that chamber's schema remain blank rather than being invented.

## 4. Open the offline labeler correctly

### In brief

- Use the desktop labeler for durable revisions and Complete.
- A directly opened static HTML can inspect and edit a local Draft, but cannot make an audited Complete revision.
- The local service binds only to `127.0.0.1` and uses a random session token.

Double-click `Start RHEED Post-processing Labeler.cmd`. Confirm the **Build** and **Open / Validate** tabs and verify that no Growth Monitor or instrument program starts.

![Offline labeler Build tab](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/rheed_labeler_build.png)

The desktop labeler starts a loopback-only local service for the selected report. This service gives the browser a narrow route to record audited revisions against the selected local report. It is not an instrument server and must not bind to a network interface.

If `interactive_report.html` is opened directly with `file:///`, navigation and local Draft editing remain available. **Complete** must explain that the report needs to be reopened through the desktop labeler. Equalizer is a separate diagnostic and is not shown or requested in the event-labeling workflow.

## 5. Prepare the archive and understand event sources

### In brief

- Preserve the original ZIP and let the tool verify every saved frame and hash.
- The explicit initial state, manual marks, automatic change candidates, and posthoc events become reviewable Drafts.
- Adjustment, image-unusable, and sensor records remain read-only context.

The archive must contain one session metadata record, its acquisition CSV files, and every referenced frame. Read `chamber_id` from the session metadata and confirm it names the intended chamber. For Modbus sessions, preserve COM, baud, RTS, device ID, and backend. For ADS sessions, preserve endpoint, ports, and cell count. For Elog sessions, preserve the configured directory and resolved source file. Never edit an archive to make it resemble the other chamber.

### Initial state and editable point-event sources

Every run begins with the explicit default state `1x1 present` and pattern
clarity `unknown`. This is the starting state for replay, not a fabricated
physical `1x1 appeared` event. Its audit item owns the first state interval's
Anchor.

| Source | Meaning |
| --- | --- |
| `manual` | A grower pressed **MARK EVENT** during acquisition. The live click creates a Draft without opening a dialog. |
| `auto_capture` | The translation-insensitive image-change detector recorded a candidate change point. It identifies a visual change, not its physical cause, and must be Confirmed or Rejected. |
| `posthoc` | A reviewer adds a point at an actual saved frame after the run. |

Future live and posthoc events use UUIDs. Older rows receive deterministic IDs derived from session identity, source file, source index, time, and capture sequence, so identical `event_idx` values in different CSV files cannot collide.

### Read-only reference sources

RHEED direction, beam current, and beam energy adjustments are displayed as reference points. An **image unusable for analysis** record means only that acquisition or capture made that frame unsuitable for interpretation; it must never be read as poor surface or film quality. Temperature, voltage, current, pressure, and data-age values are read from the original logs near the event time. They are context, not editable label fields.

Legacy `events_labels.csv` can seed old automatic-event review fields. A legacy `live_labels.csv` row is linked only when capture sequence and frame hash identify exactly one event. Ambiguous rows remain unlinked for manual resolution. Legacy temporal-segment reports remain readable but are not automatically converted because a segment has no unique scientific point.

## 6. Build and open a point-event report

### In brief

1. Select the preserved ZIP and correctly paired model inputs.
2. Build into a new or empty directory outside Git.
3. Keep the full report directory and the read-only source archive together.

Select the session ZIP, add each prediction CSV beside its matching model-spec JSON, choose a new output directory, and click **Build report and open**. Prediction rows must match the saved-frame order, time, capture sequence, filename, and SHA-256. Any mismatch stops the build.

```powershell
Set-Location '<GUI repository>'
conda activate ai4mbe-gui
python -m tools.rheed_postprocessing_labeling build `
  --session '<local-data-root>\growth_session.zip' `
  --predictions '<local-data-root>\predictions.csv' `
  --model-spec '<local-data-root>\model_spec.json' `
  --output-dir '<local-data-root>\point-event-review'
```

Keep `interactive_report.html`, `images/`, `vendor/`, `run_manifest.json`, and `annotations/` together. Review WebP images are for display only; the original saved-frame bytes remain in the read-only ZIP.

## 7. Review and complete point events

### In brief

1. Select an Unfinished event or add a posthoc event at the playhead.
2. Confirm or reject a candidate, edit its concise labels, and identify the reviewer.
3. Select the representative interval Anchor, save the Draft, then explicitly click Complete.

There are no **Mark In**, **Mark Out**, interval-overlap, or inclusive-end rules in `rheed-point-events-v2`. Each editable change is a point. Multiple events may share one timestamp or one saved frame and still retain different IDs. The timeline has three separate tracks: draggable event points, automatically derived full-state intervals, and one Anchor per interval.

### Original point and review point

The **original point** is immutable evidence: event ID, source, original time, original frame if available, capture sequence, image SHA-256, original note, and source-row SHA-256. The **review point** is the editable event location and may move only by snapping to an actual saved frame. Moving it never changes the original point. The timeline therefore preserves both what was recorded live and where the reviewer finally places the event.

### Concise semantic labels

Each event can contain one or more editable labels:

- **Reconstruction appeared** or **Reconstruction disappeared**: `1x1`, Twinned `2x1`, `c(6x2)`, RT13, or HTR.
- **Pattern clarity became**: **Good** or **Bad**.

Use the smallest set that describes the observed change. A point can carry more than one label when changes occur together. Do not use Good/Bad for image usability, instrument validity, chemical surface quality, or FeSe film quality.

Within one event, each reconstruction may appear **or** disappear only once, and there may be only one pattern-clarity change: Good **or** Bad. Record genuinely separate observations as separate event IDs; they may share the same timestamp. Same-frame labels are applied atomically. Across the run, the replay rejects repeated appearances, disappearance of an absent reconstruction, and no-op Good-to-Good or Bad-to-Bad changes. Rejecting a candidate clears its semantic labels and interval Anchor so it cannot be exported as a physical training target, while the immutable source evidence and revision history preserve what was reviewed.

### Candidate decisions and the initial state

Every automatic-change candidate must be explicitly **Confirmed** or **Rejected**. Confirming means the reviewer accepts the event and supplies its semantic label or labels. Rejecting means the detector point is retained as evidence but is not treated as a physical event. Review the initial-state item separately; it establishes the default first interval and must not be counted as a physical appearance event.

Accepted changes are replayed from the initial state. Each boundary produces a half-open interval `[current boundary, next boundary)` whose exported record contains the complete reconstruction-presence set and the current Good/Bad/unknown clarity. Training code should consume these materialized states rather than treating an individual `appeared` label as a full-frame class.

### Representative interval Anchor

The star-shaped **Anchor** belongs to a derived state interval and selects the saved frame that most clearly represents that complete state. It is not a second event and is exported under the interval. Drag or set it within that interval only. Events at the same time share one following interval and one Anchor. Moving a boundary invalidates an Anchor that no longer lies in its interval and returns the affected review to Draft.

Each interval has exactly one Anchor slot. It may be empty only while the
interval is Unfinished; duplicate Anchors are invalid, and Complete requires
that the slot identify one actual saved frame inside the interval.

### Required review actions

1. Select a Draft from **Unfinished**.
2. Inspect the original and current review markers.
3. Move the review point if a nearby saved frame better represents the event.
4. Review the initial-state item, or choose **Confirmed** or **Rejected** for an automatic candidate.
5. Add, edit, or remove reconstruction and pattern-clarity labels. For a confirmed event, retain at least one valid semantic label.
6. Set the representative interval Anchor on an actual saved frame.
7. Enter the reviewer. A confidence value and comment are optional.
8. Click **Complete** explicitly.

![Point-event editor with generated demo inputs](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/rheed_timeline_editor.png)

The actual editor uses event-point, derived-state, and interval-Anchor tracks plus an **Unfinished** queue; the former hand-drawn interval controls are absent.

For a confirmed change event, Complete is disabled until it has a reviewer, at least one valid semantic label, and a representative interval Anchor. The initial-state audit item requires a reviewer and first-interval Anchor but no change label. A rejected automatic candidate needs the reviewer and explicit Rejected decision, but no semantic label or interval Anchor. Comments are optional. Editing completed review content returns it to Draft. Use **Reopen** when intentionally continuing review.

Source events cannot be deleted. They may be marked **Dismissed** only with a reason, preserving the original evidence. A posthoc event may be deleted with an audited reason and later restored.

## 8. Interpret Anchors, logs, and display controls

### In brief

- The event point and representative interval Anchor have different meanings and remain synchronized with the frame viewer.
- Temperature, voltage, current, pressure, and data age are read-only context.
- Brightness, contrast, zoom, and image enlargement never modify archived pixels.

The draggable event marker sets the reviewed change point. The state track is recalculated from all accepted changes, beginning from the default `1x1` state. The star-shaped Anchor selects the clearest representative frame inside one resulting interval. Moving either control snaps to an actual saved frame and preserves immutable source evidence. The Anchor is training metadata for the interval; it does not create an additional appearance, disappearance, or clarity change.

![Enlarged frame view with adjustable timeline](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/rheed_timeline_zoom.png)

The playhead, image, model guides, event markers, and enlarged-view timeline must remain synchronized. Brightness and contrast alter display only. The source frame bytes and SHA-256 never change.

Instrument context is looked up from original logs at the event time. Data age helps judge how old a cached reading was, but shared display time does not prove simultaneous physical sampling. Do not copy these readings into comment or reconstruction fields merely to duplicate the logs.

## 9. Preserve revisions, drafts, and exports

### In brief

- Every edit is append-only and records actor, UTC, action, before/after state, and base revision.
- The ZIP stays read-only; offline changes live in the adjacent `annotations/` sidecar.
- JSON is canonical; CSV is a collaboration convenience.

Live sessions retain the acquisition CSVs, `rheed_event_revisions.jsonl`, and a rebuildable current-state summary. Offline reports write the same revision concept beside the report, never into the ZIP. A pending transaction marker allows an interrupted write to be recovered exactly once.

Browser local storage is only a convenience Draft. Export JSON frequently. Export CSV for meetings or review tables, but retain JSON for complete provenance and revision history. Import must verify dataset identity, ordered frame hashes, model context, event IDs, review points, representative interval Anchors, semantic labels, candidate decisions, and revision chain before replacing the current Draft.

```powershell
python -m tools.rheed_postprocessing_labeling validate `
  --report '<local-data-root>\point-event-review\interactive_report.html' `
  --annotations '<local-data-root>\point-event-review\annotations\point_events.json'
```

Old `rheed-temporal-segments-v1` JSON remains available to its original validator as read-only compatibility data. Do not reinterpret segment start, middle, or end as an event without scientific review.

## 10. Follow data and scientific safety boundaries

### In brief

- Keep real archives, images, predictions, reports, annotations, and checkpoints out of Git.
- Treat visible-model review as assisted annotation, not blind-gold truth.
- Never infer surface or film quality from an image-unusable flag.

Track tool code, tests, templates, example specifications, and sanitized screenshots. Keep experiment ZIP files, raw images, logs, predictions, generated reports, annotation sidecars, checkpoints, credentials, and unpublished results outside Git by default.

Model plots are visible during this workflow, so the result is model-assisted review and is not blind-gold. Model argmax must not overwrite a human label. Image acquisition quality describes analyzability only and is separate from the event label **Pattern clarity became Bad**. The current reconstruction target is bare STO before growth and is not FeSe film quality. Equalizer, when used elsewhere as a diagnostic, is separate and is not displayed or stored as an event-label input.

Preserve the source ZIP SHA-256, report manifest, revision journal, canonical JSON, model specifications, and complete report directory during handoff.

## 11. Troubleshoot without destroying evidence

### In brief

Record the exact error, time, chamber, launcher, commit, environment, paths, and validity state before changing anything. Never repair one chamber by copying settings from the other.

| Symptom | Safe action |
| --- | --- |
| Shortcut is missing, opens an old checkout, or has an old icon | Run `Install AI4MBE Desktop Shortcuts.cmd` from the intended current checkout; verify the shared launcher path and bundled icon. |
| Wrong chamber title | Close normally and use the matching chamber launcher. |
| Launcher reports an optional live driver missing | Preserve the launcher log and do not ARM a production mode that needs that driver; do not install packages during an operating run. |
| Direct exposure is refused | Stay idle. For a nonzero request, verify Full camera access and that kSA or Vimba X Viewer has released the camera; never bypass readback or restoration checks. |
| START remains disabled or the frame counter stalls | DISARM and inspect camera ownership, triggering, sequence, arrival age, and the exact worker error. Do not treat the last displayed frame as fresh. |
| Temperature or instrument value invalid | Record mode, connected/valid/error state, sequence, and age; do not guess COM or setpoint values. |
| Build rejects inputs | Check the ZIP, positional prediction/spec pairs, hashes, and empty output directory. |
| HTML has no images | Restore the complete report directory. |
| Unfinished event is missing | Verify the source CSV row and source-row hash, then inspect revision replay errors. |
| Automatic candidate remains Pending | Inspect the candidate and explicitly choose Confirmed or Rejected; never infer the choice from the detector score. |
| Representative Anchor is refused | Move it to an actual saved frame inside the stable interval before the next active, non-rejected semantic event. |
| Complete is disabled | Supply the reviewer and, for a confirmed event, at least one valid semantic label plus a representative interval Anchor; a rejected candidate needs an explicit Rejected decision. |
| Revision recovery fails | Preserve JSONL and pending marker; do not hand-edit them. |

For O-MBE, confirm **Oxide MBE Growth Monitor** and the intended repository, branch, commit, Python interpreter. For Ch-MBE, confirm **Chalcogenide MBE Growth Monitor** and the intended repository, branch, commit, Python interpreter.

## 12. Verify versions and finish the checklist

### In brief

Record versions and hashes, use only the selected chamber checklist, validate the point-event export, and stop when identity, provenance, or instrument state cannot be explained.

```powershell
Set-Location '<GUI repository>'
git status --short --branch
git rev-parse HEAD
conda activate ai4mbe-gui
python --version
python -m tools.rheed_postprocessing_labeling --help
Get-FileHash '<local-data-root>\growth_session.zip' -Algorithm SHA256
```

### O-MBE startup checklist

- Use only `Start O-MBE Growth Monitor.cmd` and confirm **Oxide MBE Growth Monitor**.
- Confirm the launcher log identifies O-MBE, the intended repository, branch, commit, Python, a passed chamber preflight, and optional-driver status.
- Confirm `vimba / modbus / ads / elog` against the O-MBE record.
- Confirm the O-MBE direct-exposure request, Full-access requirement when writing, and reported readback. Do not borrow Ch-MBE's exposure.
- Confirm an advancing RHEED capture sequence and current frame, valid instrument states, data age, and intended log directory.
- ARM only under the O-MBE SOP and authorized operator.

### Ch-MBE startup checklist

- Use only `Start Ch-MBE Growth Monitor.cmd` and confirm **Chalcogenide MBE Growth Monitor**.
- Confirm the launcher log identifies Ch-MBE, the intended repository, branch, commit, Python, a passed chamber preflight, and optional-driver status.
- Confirm `vimba / modbus / ads / elog` against the Ch-MBE record.
- Confirm the Ch-MBE direct-exposure request, Full-access requirement when writing, and reported readback. Do not borrow O-MBE's exposure.
- Confirm an advancing RHEED capture sequence and current frame, valid instrument states, data age, and intended log directory.
- ARM only under the Ch-MBE SOP and authorized operator.

### Shared offline checklist

- Confirm session metadata names the intended chamber.
- Preserve the source ZIP and SHA-256.
- Open through the desktop labeler for durable revisions and Complete.
- Resolve every item in Unfinished or dismiss it with a documented reason.
- Confirm every automatic candidate has an explicit Confirmed or Rejected
  decision. The initial state is fixed, not a candidate.
- Complete the initial-state audit with its reviewer and first-interval Anchor;
  do not add an appearance label to it.
- Confirm every Complete accepted event has a reviewer, semantic label, representative interval Anchor, and revision history; comments remain optional.
- Export JSON, validate it against the original report, and hand off the complete directory.
