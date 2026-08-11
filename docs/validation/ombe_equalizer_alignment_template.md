# O-MBE Equalizer Camera Alignment Validation

## Run Identity

- Date/time and operator:
- Git branch and commit:
- Workstation: Bulbasaur
- Windows version and display DPI/scaling:
- Growth Monitor/Python versions:
- kSA version and detached Live Video title:
- Session ID:
- Camera backend, HWND, and source dimensions:
- ROI/crop configuration:
- `view_segment_id` / `visual_history_generation`:
- Basis bundle ID and SHA-256:
- Calibration ID(s):
- Raw artifact directory (outside Git):
- Raw-data SHA-256 manifest:
- Managed-job status directory for the one-hour run:

## Preconditions

- [ ] Active session; WGC is connected and producing fresh sequences.
- [ ] Physical `gun_aligned=true`; no realignment is active.
- [ ] Four active canonical classes are `1x1`, `Tw(2x1)`, `c(6x2)`, and
  `RT13`.
- [ ] HTR is disabled/N/A, excluded from Auto-fit, and saved as blank.
- [ ] Full frames and long logs remain outside Git.

## Calibration Checks

Record one row for each accepted or rejected candidate.

| Path | Parity | Point method/order | RMS px | Max px | Scale | Rotation deg | Coverage % | Correlation | Result / reason |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| Automatic | | | | | | | | | |
| Manual | | left-centre-right | | | | | | | |

- [ ] Calibrate preserved the source RGB, 128 x 96 gray image, sequence,
  timestamps, HWND/backend, dimensions, session, QC segment, and generation
  while new camera callbacks arrived.
- [ ] Both normal and mirrored/end-point hypotheses were reviewable.
- [ ] Backend/HWND/dimensions/ROI/DPI changes during candidate review cleared
  the frozen attempt; changing away and back did not revive it.
- [ ] Preview used live=green and the selected warped 1x1/Tw/c(6x2)/RT13
  basis=magenta, with points and residual vectors visible.
- [ ] Acceptance was blocked unless RMS <= 3 px, max residual <= 5 px, scale
  0.5-2.0, coverage >= 50%, and the matrix was finite and invertible.
- [ ] No candidate was accepted automatically.

### Handedness Evidence

- Natural asymmetric feature: Tw / c(6x2) / RT13 / streak or tail:
- Selected parity and rationale:
- Evidence image path:
- Evidence image SHA-256:
- Evidence kind stored in `equalizer_calibrations.jsonl`:
- Independent grower confirmation:

## Live and Events Checks

- [ ] Live Auto-fit used only the four warped canonical basis images.
- [ ] Save used one immutable frame/provenance snapshot and recomputed age
  from its monotonic receive time.
- [ ] `live_labels.csv` referenced a journaled accepted calibration and wrote
  `recon_HTR` blank.
- [ ] The event frame was resolved by exact filename in
  `capture_manifest.csv`.
- [ ] Events reused its recorded compatible calibration ID, or required a new
  calibration against that exact frame.
- [ ] Missing/malformed/duplicate manifest data and incompatible calibration
  failed closed; no raw-basis fallback occurred.
- [ ] `events_labels.csv` retained the event key and capture provenance.
- [ ] No rejected save created an orphan image or incremented a label counter.
- [ ] `equalizer_calibrations.jsonl` contains parsable acceptance and
  invalidation records with full matrix, points, residuals, parity, basis ID,
  frame provenance, and QC context.
- [ ] The first accepted record for each basis ID contains one complete,
  hash-valid basis manifest; later records reference it without duplication.
- [ ] All capture/journal provenance timestamps parse as explicit UTC.

## Lifecycle and WGC Checks

| Action | Expected result | Detection time | Observed result | Pass |
|---|---|---:|---|---|
| Cover Live Video | Captured frame is not contaminated | | | |
| Move / foreground switch | Capture and accepted context remain valid | | | |
| Resize / DPI or ROI change | Calibration invalidated; Save disabled | | | |
| Begin/end gun realignment | Calibration invalidated; Save disabled | | | |
| Minimize or close Live Video | Frame cleared; calibration invalidated | | | |
| Disconnect/reconnect camera | Fresh frame required; recalibration required | | | |
| Backend or HWND change | Calibration invalidated | | | |
| New session | Previous calibration unavailable | | | |
| Basis bundle/hash change | Calibration invalidated | | | |

## Unexpected GUI Close

- Close method and timestamp:
- [ ] CSV and JSONL files remained parsable to their last complete record.
- [ ] No image existed without its corresponding CSV row.
- [ ] Any pending Live-label, calibration evidence/journal, and auto-capture
  buffer/event-row WAL was automatically completed or rolled back, while
  malformed/conflicting WAL state failed closed and was retained.
- [ ] Capture, classifier, and sensor workers exited within the expected
  shutdown deadline; no orphan process/thread remained.
- [ ] Restart did not reuse the previous session's accepted calibration.

## One-Hour Stability

| Metric | Result |
|---|---|
| Duration / frames received | |
| Sequence stalls or stale gaps | |
| Calibration invalidations | |
| Labels attempted / accepted / rejected | |
| RSS start / end / trend | |
| Python/native threads start / end | |
| Unparsable CSV/JSONL records | |

## Result and Evidence

- Overall outcome: PASS / FAIL
- Deviations from procedure:
- Representative overlay paths and SHA-256:
- Summary CSV/JSON paths:
- Findings and recommended follow-up:

Instrument power loss and serial/PC disconnect are recorded in the
`codex/ombe-read-failure-validation` report. They remain part of the overall
machine gate, but do not alter this alignment algorithm's acceptance limits.
