# O-MBE and Ch-MBE GUI and RHEED Point-event Review

## English Operator Manual

- Manual version: **v2.0 source draft**
- Issued: **2026-08-17**
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
- Equalizer is an auxiliary visual fit, not a human label or model probability.

The live and offline paths share event identities but have different authority. During an experiment, **MARK EVENT** records what happened without interrupting the grower. After the experiment, the labeler shows that same event in the **Unfinished** queue so the grower can select a better saved frame, add a comment, run Equalizer, and explicitly complete the review.

The current classifier concerns the bare STO surface before growth. Keep these concepts separate:

- **Human reconstruction interpretation:** what a reviewer believes the surface pattern represents.
- **Equalizer result:** a visual linear fit using four active basis images.
- **Image acquisition quality:** whether the captured image is usable for analysis. It is not a statement about surface or FeSe film quality.
- **Model output:** a prediction displayed for reference. It must not overwrite either the human interpretation or Equalizer result.

## 2. Start the correct chamber application

### In brief

1. Use the launcher that matches the physical chamber.
2. Verify the exact title, reader modes, live validity, and log path before ARM.
3. Close normally so CSV and JSONL records finish writing.

Install shortcuts from the current checkout after cloning or moving it. Stop if the physical chamber, launcher, title, or displayed chamber identity disagree.

| Chamber | Launcher | Expected title |
| --- | --- | --- |
| Ch-MBE | `Start Ch-MBE Growth Monitor.cmd` | **Chalcogenide MBE Growth Monitor** |
| O-MBE | `Start O-MBE Growth Monitor.cmd` | **Oxide MBE Growth Monitor** |

The launchers select separate chamber profiles and single-instance locks. Their configurations are not interchangeable. Before a production launch, the selected Python must import the default Vimba, ADS, and serial drivers; O-MBE must also import `pymodbus`. A missing required driver blocks startup instead of opening a GUI whose default readers cannot work. Launcher logs under `%LOCALAPPDATA%\AI4MBE\LauncherLogs` record the resolved repository, commit, Python interpreter, chamber, required modules, and dependency result. Windows Graphics Capture is an optional diagnostic mode and is checked by the GUI only when selected.

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

- Use the desktop labeler when Equalizer or Complete is needed.
- A directly opened static HTML can inspect and edit Drafts but cannot run Equalizer or Complete.
- The local service binds only to `127.0.0.1` and uses a random session token.

Double-click `Start RHEED Post-processing Labeler.cmd`. Confirm the **Build** and **Open / Validate** tabs and verify that no Growth Monitor or instrument program starts.

![Offline labeler Build tab](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/rheed_labeler_build.png)

The desktop labeler starts a loopback-only local service for the selected report. This service gives the browser a narrow route to request the existing PyQt retrospective Equalizer and to record audited revisions. It is not an instrument server and must not bind to a network interface.

If `interactive_report.html` is opened directly with `file:///`, navigation and Draft editing remain available. **Run Equalizer** and **Complete** must show that the report needs to be reopened through the desktop labeler.

## 5. Prepare the archive and understand event sources

### In brief

- Preserve the original ZIP and let the tool verify every saved frame and hash.
- Manual marks and automatic image-change events become editable Drafts.
- Adjustment, image-unusable, and sensor records remain read-only context.

The archive must contain one session metadata record, its acquisition CSV files, and every referenced frame. Read `chamber_id` from the session metadata and confirm it names the intended chamber. For Modbus sessions, preserve COM, baud, RTS, device ID, and backend. For ADS sessions, preserve endpoint, ports, and cell count. For Elog sessions, preserve the configured directory and resolved source file. Never edit an archive to make it resemble the other chamber.

### Editable point-event sources

| Source | Meaning |
| --- | --- |
| `manual` | A grower pressed **MARK EVENT** during acquisition. The live click creates a Draft without opening a dialog. |
| `auto_capture` | The image-change detector recorded a change point. This identifies a visual change, not its physical cause. |
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

Keep `interactive_report.html`, `images/`, `vendor/`, `run_manifest.json`, and `annotations/` together. Review WebP images are for display only. Equalizer reads the original BMP or PNG bytes from the ZIP.

## 7. Review and complete point events

### In brief

1. Select an Unfinished event or add a posthoc event at the playhead.
2. Snap the review point to the best saved frame, write a comment, and identify the reviewer.
3. Run and adjust Equalizer, save the Draft, then explicitly click Complete.

There are no **Mark In**, **Mark Out**, interval-overlap, or inclusive-end rules in `rheed-point-events-v1`. Each item is a point. Multiple events may share one timestamp or one saved frame and still retain different IDs.

### Original point and review point

The **original point** is immutable evidence: event ID, source, original time, original frame if available, capture sequence, image SHA-256, original note, and source-row SHA-256. The **review point** may move, but only by snapping to an actual saved frame. Moving it never changes the original point and automatically clears the old Equalizer result because that result belonged to another image.

### Required review actions

1. Select a Draft from **Unfinished**.
2. Inspect the original and current review markers.
3. Move the review point if a nearby saved frame better represents the event.
4. Enter a meaningful comment and reviewer. Confidence and human reconstruction before/after fields are optional.
5. Click **Run Equalizer**. If no compatible accepted calibration exists for the exact frame geometry, view segment, and basis bundle, perform the three-point alignment and confirm handedness first.
6. Adjust and save the Equalizer measurement.
7. Click **Complete** explicitly.

![Point-event editor with generated demo inputs](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/rheed_timeline_editor.png)

The actual editor uses point markers and an **Unfinished** queue; the former interval-label controls are absent.

Complete is disabled until all of these are present: nonempty comment, reviewer, saved review frame, and valid Equalizer result for that exact frame. Editing a completed event's comment, review point, or Equalizer automatically returns it to Draft. Use **Reopen** when intentionally continuing review.

Source events cannot be deleted. They may be marked **Dismissed** only with a reason, preserving the original evidence. A posthoc event may be deleted with an audited reason and later restored.

## 8. Interpret Equalizer, logs, and display controls

### In brief

- Equalizer is a separate visual fit and never fills a human reconstruction field.
- Temperature, voltage, current, pressure, and data age are read-only context.
- Brightness, contrast, zoom, and image enlargement never modify archived pixels.

Equalizer records calibration ID, basis-bundle ID, raw/final/normalized weights, fit residual, valid coverage, exact frame hash, and four active classes: `1x1`, `Tw`, `c6x2`, and `RT13`. HTR has no canonical basis and must remain null. Equalizer weights are not model probabilities, area fractions, or automatic human labels.

![Enlarged frame view with adjustable timeline](../tools/rheed_postprocessing_labeling/manual/assets/screenshots/rheed_timeline_zoom.png)

The playhead, image, model guides, event markers, and enlarged-view timeline must remain synchronized. Brightness and contrast alter display only. The source frame bytes and SHA-256 never change.

Instrument context is looked up from original logs at the event time. Data age helps judge how old a cached reading was, but shared display time does not prove simultaneous physical sampling. Do not copy these readings into comment or reconstruction fields merely to duplicate the logs.

## 9. Preserve revisions, drafts, and exports

### In brief

- Every edit is append-only and records actor, UTC, action, before/after state, and base revision.
- The ZIP stays read-only; offline changes live in the adjacent `annotations/` sidecar.
- JSON is canonical; CSV is a collaboration convenience.

Live sessions retain the acquisition CSVs, `rheed_event_revisions.jsonl`, and a rebuildable current-state summary. Offline reports write the same revision concept beside the report, never into the ZIP. A pending transaction marker allows an interrupted write to be recovered exactly once.

Browser local storage is only a convenience Draft. Export JSON frequently. Export CSV for meetings or review tables, but retain JSON for complete provenance and revision history. Import must verify dataset identity, ordered frame hashes, model context, event IDs, review anchors, and revision chain before replacing the current Draft.

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

Equalizer and model plots are visible during this workflow, so the result is model-assisted review and is not blind-gold. Equalizer must not overwrite a human label, and model argmax must not overwrite either. Image acquisition quality describes analyzability only. The current reconstruction target is bare STO before growth and is not FeSe film quality.

Preserve the source ZIP SHA-256, report manifest, revision journal, canonical JSON, model specifications, and complete report directory during handoff.

## 11. Troubleshoot without destroying evidence

### In brief

Record the exact error, time, chamber, launcher, commit, environment, paths, and validity state before changing anything. Never repair one chamber by copying settings from the other.

| Symptom | Safe action |
| --- | --- |
| Wrong chamber title | Close normally and use the matching chamber launcher. |
| Temperature or instrument value invalid | Record mode, connected/valid/error state, sequence, and age; do not guess COM or setpoint values. |
| Build rejects inputs | Check the ZIP, positional prediction/spec pairs, hashes, and empty output directory. |
| HTML has no images | Restore the complete report directory. |
| Unfinished event is missing | Verify the source CSV row and source-row hash, then inspect revision replay errors. |
| Run Equalizer unavailable | Reopen the report through the desktop labeler, not `file:///`. |
| Equalizer is rejected | Verify exact raw frame, calibration compatibility, handedness, geometry, view segment, and basis hash. |
| Complete is disabled | Supply comment, reviewer, saved review frame, and valid Equalizer result. |
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
- Confirm the launcher log identifies O-MBE, the intended repository, branch, commit, Python, and a passed production-driver probe.
- Confirm `vimba / modbus / ads / elog` against the O-MBE record.
- Confirm advancing RHEED and instrument states, data age, and intended log directory.
- ARM only under the O-MBE SOP and authorized operator.

### Ch-MBE startup checklist

- Use only `Start Ch-MBE Growth Monitor.cmd` and confirm **Chalcogenide MBE Growth Monitor**.
- Confirm the launcher log identifies Ch-MBE, the intended repository, branch, commit, Python, and a passed production-driver probe.
- Confirm `vimba / modbus / ads / elog` against the Ch-MBE record.
- Confirm advancing RHEED and instrument states, data age, and intended log directory.
- ARM only under the Ch-MBE SOP and authorized operator.

### Shared offline checklist

- Confirm session metadata names the intended chamber.
- Preserve the source ZIP and SHA-256.
- Open through the desktop labeler when Equalizer or Complete is required.
- Resolve every item in Unfinished or dismiss it with a documented reason.
- Confirm every Complete event has comment, reviewer, exact-frame Equalizer, and revision history.
- Export JSON, validate it against the original report, and hand off the complete directory.
