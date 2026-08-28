# RHEED Offline Post-processing and Labeling

This self-contained tool directory turns an archived Growth Monitor session and
one or more model-prediction CSV files into a local interactive point-event
timeline. It is independent of live acquisition and never connects to cameras
or instruments. The desktop launcher serves only the selected report on
`127.0.0.1` with a random session token; it has no external network runtime
dependency. All implementation, templates, offline assets, tests, and usage
instructions live under `tools/rheed_postprocessing_labeling/`.

## Data is distributed separately

This tool directory contains no laboratory session archive, RHEED frame, prediction
table, sensor log, generated report, or model checkpoint. Collaborators must
obtain the authorized dataset separately and keep it outside the repository.
Only the reusable code, templates, documentation, and runtime-generated
synthetic tests belong in Git.

## Inputs

The session ZIP must contain one `session_metadata.json`, one
`heartbeat_log.csv`, and its referenced frames under `frames/`. Prediction CSV
rows must have this provenance prefix:

```text
frame_index,heartbeat_idx,elapsed_s,captured_at_utc,capture_sequence,frame_name,frame_sha256
```

Rows must cover the same saved frames in the same order as the heartbeat log.
`frame_index` may start at 0 or 1, but it must then remain contiguous; the
reader detects that convention and validates every other provenance field.
Skipped heartbeat indices, capture-sequence gaps, and irregular elapsed times
are preserved; the tool never assumes one frame per second.

Each prediction CSV is paired positionally with one model-spec JSON. The spec
declares the model key, ordered class names, probability columns, optional
quality/predicted columns, display text, status, and provenance. Copy
`tools/rheed_postprocessing_labeling/model_spec.example.json` and update it for
each model. Do not claim a deployment or evaluation status that its manifest
does not support.

## Build a report

### Desktop launcher (recommended on Windows)

Double-click `Start RHEED Post-processing Labeler.cmd` in the repository root,
or install the desktop icons once with `Install AI4MBE Desktop Shortcuts.cmd`.
The launcher keeps each prediction CSV explicitly paired with its model-spec
JSON, runs report generation in a separate process, and opens the report only
after a successful build. Choose a new output directory outside the Git clone;
the desktop workflow never overwrites an existing non-empty directory.

The same launcher is available from PowerShell:

```powershell
python -m tools.rheed_postprocessing_labeling desktop
```

To preload an existing report with its matching immutable archive and open it
through the durable local service in one command:

```powershell
python -m tools.rheed_postprocessing_labeling desktop `
  --session 'D:\RHEED-local\growth_session.zip' `
  --report 'D:\RHEED-local\labeling-output\interactive_report.html' `
  --open
```

The default English-only PDF operator guide for both O-MBE and Ch-MBE is
[`docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf`](../../docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf).
It is also available as searchable
[Markdown](../../docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.md),
with a separate copy-paste
[AI prompt pack](../../docs/RHEED_GUI_Postprocessing_Labeling_AI_Prompt_Pack_EN.md).
Every numbered chapter begins with an `In brief` summary before the detailed
instructions. Shared procedures appear once; chamber-specific launch,
configuration, troubleshooting, and checklist sections cross-reference the
matching chamber record.
Its LaTeX source and reproducible Windows build helper are:

```powershell
.\tools\rheed_postprocessing_labeling\manual\build_english_manual.ps1
```

The historical
[bilingual v1 PDF](../../docs/RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf)
and its ReportLab generator remain only for interpreting read-only temporal-
segment exports. They do not describe the current point-event workflow and
must not be used as operating instructions. The English PDF uses actual
application screenshots captured with generated demo inputs; neither edition
contains laboratory data. The English manual keeps the Ch-MBE and O-MBE
startup configurations separate. Demo screenshots are not production evidence
or permission to ARM.

### Command line

Run from the GUI repository root in PowerShell:

```powershell
conda activate ai4mbe-gui

$Session = 'D:\RHEED-local\growth_session.zip'
$Predictions = 'D:\RHEED-local\predictions.csv'
$ModelSpec = 'D:\RHEED-local\model_spec.json'
$Output = 'D:\RHEED-local\labeling-output'

python -m tools.rheed_postprocessing_labeling build `
  --session $Session `
  --predictions $Predictions `
  --model-spec $ModelSpec `
  --output-dir $Output

# Direct file opening is local-Draft-only. Use the desktop launcher for
# durable revisions and Complete.
Start-Process (Join-Path $Output 'interactive_report.html')
```

For multiple models, pass equal-length, positionally paired lists:

```powershell
python -m tools.rheed_postprocessing_labeling build `
  --session $Session `
  --predictions $Predictions4 $Predictions5 `
  --model-spec $ModelSpec4 $ModelSpec5 `
  --output-dir $Output `
  --title 'Annealing review'
```

The output contains `interactive_report.html`, local JavaScript, local review
images, `run_manifest.json`, and an `annotations/` sidecar once edits are
saved. Keep the complete directory together. Review images are lossy display
copies; original saved-frame bytes remain in the read-only ZIP.

## Review point events

The current schema is `rheed-point-events-v3`. It begins from an explicit
initial state: `1x1` present and pattern clarity unknown. That state is not a
physical appearance event. Its review record only provides audit and Anchor
ownership for the first interval. The editable change-event sources are:

- `manual`: the grower pressed **MARK EVENT** during acquisition. The click
  stays one-step and immediately creates a Draft, even with no comment.
- `auto_capture`: the translation-insensitive image-change detector recorded a
  visual change. It does not determine the physical cause or reconstruction.
  Each automatic point is a candidate that must be Confirmed or Rejected.
- `posthoc`: a reviewer adds an event at an actual saved frame after the run.

Legacy `rheed-point-events-v2` documents remain validated, byte-preserving
read-only evidence. Their deprecated `surface_quality` values are never mapped
to v3 pattern clarity; create a separate v3 sidecar to continue annotation.

RHEED direction, beam-current, and beam-energy adjustments are read-only
reference points. An image-unusable record means only that acquisition or
capture made that image unsuitable for analysis; it is not a statement about
surface or film quality. Temperature, voltage, current, pressure, and data age
come from the original logs and remain read-only context, not label fields.

Every event retains an immutable original point and a movable review point.
The review point can move only by snapping to an actual saved frame, without
changing the original evidence. Multiple events may share the same timestamp
or frame while retaining different stable IDs.

The concise semantic vocabulary is:

- reconstruction **appeared** or **disappeared**: `1x1`, Twinned `2x1`,
  `c(6x2)`, RT13, or HTR;
- RHEED pattern clarity **became**: Good or Bad.

Every label is editable, and one point may contain multiple labels. Good/Bad
describes visible pattern clarity only. It is not image usability, instrument
validity, chemical surface quality, or FeSe film quality. In particular, an
image-unusable record must never be converted into Bad clarity.

Within one event, each reconstruction value can appear or disappear only
once, and there can be only one clarity change (Good or Bad). Use separate
event IDs for genuinely separate observations, including observations at the
same timestamp. Rejecting a candidate clears semantic labels and the
representative Anchor while preserving immutable source evidence and the
revision's prior state.

Accepted point changes are replayed from the initial state. Every unique event
boundary automatically creates a half-open state interval whose exported state
contains the complete reconstruction-presence set and current Good/Bad/unknown
clarity, not merely the last change. Same-frame changes are applied atomically.
The editor rejects impossible histories such as a repeated appearance, a
disappearance of an absent type, or Good changing to Good.

The star-shaped representative **Anchor** belongs to one derived state interval
and selects its clearest saved frame. It is not another event. Same-time events
share one following interval and one exported Anchor. The UI exposes separate
tracks for event points, derived state intervals, and interval Anchors; it does
not ask the grower to create boundaries with Mark In/Out.

Each derived interval has exactly one Anchor slot. A Draft may temporarily have
no Anchor while it is unfinished, but it can never contain two Anchors and it
cannot be completed until that slot identifies one actual saved frame inside
the interval. Moving an event boundary invalidates an Anchor that falls outside
the recalculated interval.

Use the **Unfinished** queue as the normal review route:

1. Select an event or add a posthoc event at the playhead.
2. Move the review point when a nearby saved frame better represents the
   event.
3. Review the fixed initial-state item, or explicitly Confirm or Reject an
   automatic candidate. The initial state has no candidate decision and no
   semantic change label.
4. Add, edit, or remove its semantic labels and set the representative
   interval Anchor.
5. Identify the reviewer. Confidence and comment are optional.
6. Save the Draft and explicitly click **Complete**.

For a confirmed event, Complete requires a reviewer, at least one valid
semantic label, and an Anchor for its derived interval. The initial-state
review requires its first-interval Anchor but does not fabricate a change.
A rejected candidate requires the reviewer and explicit Rejected decision,
but no semantic label or Anchor. Comments are optional. Editing completed
review content returns it to Draft. Source events cannot be deleted; they can
be dismissed only with a reason. Posthoc events may be deleted and restored
through audited revisions.

## Desktop revision authority

Durable revisions and **Complete** require the desktop labeler. Its local
service is bound only to `127.0.0.1` and protected by a random token. A report
opened directly as `file:///.../interactive_report.html` remains useful for
navigation and local Draft editing, but explains that audited completion is
unavailable. Equalizer, if used elsewhere as a diagnostic, is separate and is
not shown or stored as an event-label input.

## Revisions, export, and compatibility

Source ZIPs and acquisition CSVs remain unchanged. Every edit appends actor,
UTC, action, before/after state, and base revision ID to the revision journal;
the current-state summary can be rebuilt from that journal. Offline changes
live beside the report under `annotations/`. Browser local storage is only a
convenience Draft, so export often.

JSON is the canonical round-trip format. It stores the initial state, point
changes, and materialized full-state intervals with their Anchors. CSV is a
convenient collaboration table but omits nested provenance and revision
history. Import and validation replay every change and bind the dataset,
ordered frame hashes, model context, event IDs, immutable source evidence,
saved-frame review points, semantic labels, candidate decisions, derived
states, interval Anchors, revision chain, and Complete gate.

Routine review points and Anchors use stable `session_identity`,
`capture_sequence`, and a saved-frame locator (`archive_member`,
`frame_index`, and/or `frame_path`) as their primary identity. A SHA-256
already present in source evidence may be carried and checked, but report
generation, posthoc labeling, event movement, and Anchor selection never
compute a content hash solely for annotation.

The report shows model outputs, so all reviews are **model-assisted data, not
blind gold labels**. Model output must not fill human event labels. The current
classifier concerns bare STO before growth, not FeSe film quality.

Legacy `rheed-temporal-segments-v1` exports remain available to their original
validator as read-only compatibility data. They are not converted to points:
a segment start, middle, or end has no unique scientific event meaning.

Validate an exported annotation file before downstream use:

```powershell
python -m tools.rheed_postprocessing_labeling validate `
  --report (Join-Path $Output 'interactive_report.html') `
  --annotations 'D:\RHEED-local\point-event-review\annotations\point_events.json'
```

Validation fails closed on a different run, altered ordering, forged source or
review point, out-of-interval representative Anchor, invalid semantic label or
candidate decision, broken revision chain, invalid Complete state, or
gold-eligibility claim.

## Offline verification

The Python suite creates deterministic synthetic PNGs, ZIPs, predictions, and
model specs at runtime; it never opens laboratory files or loads checkpoints.

```powershell
python -m pytest tools\rheed_postprocessing_labeling\tests -q

$env:PLAYWRIGHT_EXECUTABLE_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'  # optional
node tools\rheed_postprocessing_labeling\tests\browser\verify_rheed_labeling_ui.js `
  (Join-Path $Output 'interactive_report.html') `
  (Join-Path $env:TEMP 'rheed-labeling-browser-test')
```

The browser verifier tries `playwright`, then `@playwright/test`, then
`playwright-core`; set `PLAYWRIGHT_MODULE` to an alternate installed module if
needed. It serves the report from an ephemeral loopback-only HTTP endpoint,
blocks every external request, and checks point creation/editing, candidate
decisions, semantic-label editing, representative-Anchor dragging, the
Unfinished queue, export, playhead synchronization, and responsive layouts.

`--overwrite` is an advanced CLI-only option. It accepts only a previous
output from this tool with the expected manifest and generated top-level
contents; unexpected files cause a refusal. New inputs and the staged report
are validated completely before the old report is transactionally replaced.
Filesystem roots, repository/source ancestors, the current working directory,
and directories containing an input file are always refused. Still use a
dedicated output directory and keep annotation exports elsewhere.

Keep generated reports, exported annotations, screenshots, raw sessions, and
predictions outside the repository (for example under `D:\RHEED-local`). They
may contain unpublished experiment data and are intentionally not Git inputs.
