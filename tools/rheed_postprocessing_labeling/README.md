# RHEED Offline Post-processing and Labeling

This self-contained tool directory turns an archived Growth Monitor session and
one or more model-prediction CSV files into a local interactive timeline. It is
independent of the live GUI and does not connect to cameras, instruments, or
the network. All implementation, templates, offline assets, tests, and usage
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

The default English-only PDF operator guide is
[`docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf`](../../docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf).
Its LaTeX source and reproducible Windows build helper are:

```powershell
.\tools\rheed_postprocessing_labeling\manual\build_english_manual.ps1
```

The earlier
[bilingual PDF](../../docs/RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf)
and its ReportLab generator remain available. The English PDF uses only
LaTeX/TikZ vector illustrations; both editions contain no laboratory data.

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

The output contains `interactive_report.html`, local JavaScript, and local
review images. Keep the entire output directory together. It has no CDN or
runtime network dependency.

## Label temporal segments

Move the full-session scrubber to a saved frame, then use **Mark In** and
**Mark Out**, select a reconstruction label, enter the labeler name, and add
the segment. Out is inclusive in the UI. Visible and exported saved-frame
ordinals are 1-based; prediction CSV `frame_index` may be consistently 0- or
1-based. The heartbeat index, capture sequence, UTC, and frame SHA-256
disambiguate every
endpoint. Adjacent segments are allowed; overlapping segments are rejected.

The browser keeps a dataset-scoped draft in local storage. Export JSON often;
CSV is convenient for analysis, but JSON is the canonical round-trip format.
Imported JSON must match the report's dataset ID, frame count, ordered-frame
fingerprint, exact endpoint provenance, and model-review-context fingerprint.
The latter binds the labels to the prediction files and model specifications
that were visible during review.

## Annotation safety and schema

The report shows model outputs and may use lossy review images. Therefore all
labels made here are **model-assisted review data, not blind gold labels**.
Exports must retain:

```json
{
  "schema_version": "rheed-temporal-segments-v1",
  "document_type": "ai4mbe_rheed_segment_annotations",
  "annotation_set": {
    "annotation_mode": "model_assisted_review",
    "model_outputs_visible": true,
    "eligible_for_gold": false
  }
}
```

Each segment records its stable annotation ID, human label, notes, inclusive
start/end provenance, frame count, display brightness/contrast, and audit
timestamps. QC labels remain a separate concept; do not infer acquisition QC
or film quality from reconstruction segments.

Validate an exported annotation file before downstream use:

```powershell
python -m tools.rheed_postprocessing_labeling validate `
  --report (Join-Path $Output 'interactive_report.html') `
  --annotations 'D:\RHEED-local\synthetic_session_segment_annotations.json'
```

Validation fails closed on a different run, altered ordering, forged endpoint,
overlap, malformed interval, or gold-eligibility claim.

## Offline verification

The Python suite creates deterministic synthetic PNGs, ZIPs, predictions, and
model specs at runtime; it never opens laboratory files or loads checkpoints.

```powershell
pytest tools\rheed_postprocessing_labeling\tests\test_rheed_postprocessing_labeling.py -q

$env:PLAYWRIGHT_EXECUTABLE_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'  # optional
node tools\rheed_postprocessing_labeling\tests\browser\verify_rheed_labeling_ui.js `
  (Join-Path $Output 'interactive_report.html') `
  (Join-Path $env:TEMP 'rheed-labeling-browser-test')
```

The browser verifier tries `playwright`, then `@playwright/test`, then
`playwright-core`; set `PLAYWRIGHT_MODULE` to an alternate installed module if
needed. It serves the report from an ephemeral loopback-only HTTP endpoint,
blocks every external request, and checks editing, overlap rejection, export,
playhead synchronization, and 1024/736/360-pixel layouts.

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
