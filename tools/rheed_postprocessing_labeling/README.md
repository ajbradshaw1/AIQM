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

The earlier
[bilingual PDF](../../docs/RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf)
and its ReportLab generator remain available. The English PDF uses actual
application screenshots captured with generated demo inputs; neither edition
contains laboratory data. The English manual keeps the Ch-MBE and O-MBE
startup configurations separate. Demo screenshots are not production
evidence or permission to ARM.

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

# Direct file opening is view/Draft-only. Use the desktop launcher when
# Run Equalizer or Complete is required.
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
copies; Equalizer reads the original BMP or PNG from the read-only ZIP.

## Review point events

The current schema is `rheed-point-events-v1`. It has three editable event
sources:

- `manual`: the grower pressed **MARK EVENT** during acquisition. The click
  stays one-step and immediately creates a Draft, even with no comment.
- `auto_capture`: the image-change detector recorded a visual change. It does
  not determine the physical cause or reconstruction.
- `posthoc`: a reviewer adds an event at an actual saved frame after the run.

RHEED direction, beam-current, and beam-energy adjustments are read-only
reference points. An image-unusable record means only that acquisition or
capture made that image unsuitable for analysis; it is not a statement about
surface or film quality. Temperature, voltage, current, pressure, and data age
come from the original logs and remain read-only context, not label fields.

Every event retains an immutable original point and a movable review point.
The review point can move only by snapping to an actual saved frame. Moving it
does not change the original evidence and clears the previous Equalizer result
because that result belonged to another image. Multiple events may share the
same timestamp or frame while retaining different stable IDs.

Use the **Unfinished** queue as the normal review route:

1. Select an event or add a posthoc event at the playhead.
2. Choose the best saved-frame review point.
3. Add a nonempty comment and reviewer. Confidence and human reconstruction
   before/after fields are optional and remain editable.
4. Run and adjust Equalizer for that exact frame.
5. Save the Draft and explicitly click **Complete**.

Complete requires the comment, reviewer, saved review frame, and valid
Equalizer result. Editing a completed event's comment, review point, or
Equalizer returns it to Draft. Source events cannot be deleted; they can be
dismissed only with a reason. Posthoc events may be deleted and restored
through audited revisions.

## Equalizer and desktop authority

Equalizer is an independent four-basis visual fit. It records calibration and
basis IDs, raw/final/normalized weights, residual, coverage, and exact frame
hash for `1x1`, `Tw`, `c6x2`, and `RT13`. HTR has no canonical basis and stays
null. Equalizer weights are not model probabilities, area fractions, or human
reconstruction labels, and they never populate human reconstruction fields.

**Run Equalizer** and **Complete** require the desktop labeler. Its local
service is bound only to `127.0.0.1` and protected by a random token. A report
opened directly as `file:///.../interactive_report.html` remains useful for
navigation and Draft editing but explains that those two actions are
unavailable. No compatible calibration means the reviewer must complete the
existing three-point alignment and handedness confirmation first.

## Ch-MBE offline performance probe

Before using the point-event editor on the older Ch-MBE workstation, measure
the real local report and ZIP with the single-event probe:

```powershell
conda activate ai4mbe-gui
python -m tools.rheed_postprocessing_labeling performance-probe `
  --report D:\path\to\report\interactive_report.html `
  --session D:\path\to\growth_session.zip `
  --edit-iterations 50 `
  --output D:\path\to\results\chmbe_point_event_performance.json
```

The probe verifies and indexes the source ZIP, decodes only the selected
event's exact raw frame, constructs the real retrospective Equalizer widget,
and loads exactly four active simulator bases. It never imports a classifier,
runs model inference, contacts an instrument, or precomputes the full image
sequence. Repeated event edits are written only beside a temporary report
copy; the source ZIP and real annotation sidecar remain unchanged. The JSON
reports startup, archive verification, single-frame decode, Equalizer
preparation and four-basis numerical-kernel times, edit latency percentiles,
sampled process peak memory, Python allocation peak, and memory/latency drift.
Use `--event-id <UUID>` to benchmark a specific event; otherwise the earliest
active labelable event is used.

## Revisions, export, and compatibility

Source ZIPs and acquisition CSVs remain unchanged. Every edit appends actor,
UTC, action, before/after state, and base revision ID to the revision journal;
the current-state summary can be rebuilt from that journal. Offline changes
live beside the report under `annotations/`. Browser local storage is only a
convenience Draft, so export often.

JSON is the canonical round-trip format. CSV is a convenient collaboration
table but omits nested provenance and revision history. Import and validation
bind the dataset, ordered frame hashes, model context, event IDs, immutable
source evidence, saved-frame review points, revision chain, Complete gate, and
Equalizer frame hash.

The report shows model outputs, so all reviews are **model-assisted data, not
blind gold labels**. Model output, Equalizer, and human reconstruction remain
separate. The current classifier concerns bare STO before growth, not FeSe
film quality.

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
review point, broken revision chain, invalid Complete state, mismatched
Equalizer frame, or gold-eligibility claim.

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
blocks every external request, and checks point creation/editing, the
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
