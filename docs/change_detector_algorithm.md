# Translation-insensitive RHEED change proposals

Status: production candidate for grower review. The detector proposes event
points; it does not assign reconstruction or pattern-clarity labels.

## In brief

The live Growth Monitor now registers each RHEED frame to a rolling reference
using a bounded two-dimensional translation before it measures visual change.
Small camera or beam-image shifts therefore do not become reconstruction
events. A proposal still requires persistence, an adaptive threshold, and a
cooldown. Every proposal remains `pending` until a grower confirms or rejects
it in the event editor.

## What the detector is for

The detector answers one narrow question: **did the recorded RHEED pattern
change enough to deserve review at this time?** It deliberately does not infer:

- which reconstruction appeared or disappeared;
- whether the pattern became Good or Bad;
- whether the surface is physically better or worse;
- whether an annealing command should be issued.

Those meanings require a grower label and the recorded temperature, power,
pressure, and history. Image-unusable records such as obstruction or a camera
failure are separate read-only context, not a Bad surface label.

## Processing pipeline

`gui.auto_capture.TranslationInvariantChangeDetector` performs these steps for
each valid frame:

1. Convert the input to a two-dimensional floating-point image. Existing
   false-colour inputs retain the established green-channel convention.
2. Form a per-pixel rolling reference from the previous 20 aligned frames.
   Validity masks keep padded borders out of both the reference and score.
3. Estimate translation by windowed phase correlation on a down-sampled image.
4. Refine the strongest bounded candidate using normalized correlation on
   real, non-wrapped overlap pixels.
5. Reject low-texture frames, insufficient overlap, and convincing motion
   beyond the configured 12-pixel bound. These are zero-score, fail-closed
   states with an explicit diagnostic reason.
6. Robustly normalize the 5th-to-95th percentile luminance range of the
   registered current/reference overlap independently. This reduces uniform
   brightness and contrast sensitivity.
7. Compute the winsorized mean absolute residual on the valid overlap and map
   it to the familiar approximate 0-255 scale.
8. Smooth the last three residual scores.

The detector estimates translation only. It never fits scale or rotation, so a
true geometry change is not silently normalized away.

## Live proposal policy

`AutoCaptureEngine` adds temporal gates to the detector score:

- 20 fixed warmup frames fill the visual reference;
- 20 additional scores fill the adaptive baseline without firing;
- the threshold is `max(0.5, baseline mean + 3 * baseline standard deviation)`;
- three consecutive above-threshold frames are required;
- after a proposal, three consecutive below-threshold frames are required to
  rearm the detector; a sustained high-score plateau therefore produces one
  proposal rather than one proposal per cooldown window;
- a 10-second cooldown remains a second guard against nearby retriggers;
- only non-trigger scores update the 100-score adaptive baseline.

These values are the current operating defaults, not universal physical
constants. Revalidate them when camera cadence, image geometry, palette, or
beam-view conditions change.

## Logged evidence

Every automatic event row and every saved context-frame manifest now includes:

- detector name and status;
- final and raw change scores;
- registered x/y translation in pixels;
- registration confidence and valid overlap fraction;
- the effective threshold, armed state, trigger state, and any suppression
  reason;
- exact capture sequence, time, backend, geometry, and saved frame path.

The original acquisition CSV remains evidence. Grower review is stored through
the separate append-only point-event revision journal.

## Human review

An automatic candidate starts as `pending`. The grower must choose one of:

- `confirmed`: keep it and add one or more semantic changes;
- `rejected`: record that the detector proposal was not a useful event.

For a confirmed event, labels are intentionally small:

- reconstruction type `appeared` or `disappeared` for 1x1, Twinned 2x1,
  c(6x2), RT13, or HTR;
- pattern clarity `became Good` or `became Bad`.

The event boundary and the representative Anchor are separate saved frames.
The boundary indicates where the change is reviewed; the Anchor is the clearest
example in the stable interval after that change.

## Failure modes and interpretation

- A zero score with `low_texture`, `insufficient_overlap`, `excessive_shift`,
  or `invalid_frame` is unavailable evidence, not proof that nothing changed.
- Temperature-dependent intensity changes can survive registration and may
  legitimately produce proposals without a reconstruction transition.
- Direction, RHEED current, RHEED energy, obstruction, and camera-geometry
  changes remain read-only reference markers. They are not learned semantic
  event labels.
- Several 1 Hz frames from one run are correlated observations, not independent
  training examples. Detector evaluation and model splits must be by complete
  annealing run.

## Validation

Unit tests inject pure translations, brightness/contrast changes, structural
changes, low-texture frames, excessive shifts, and detector reset conditions.
The production replay utility streams a session ZIP without extraction:

```powershell
python scripts/replay_translation_change_detector.py `
  D:\path\to\session.zip `
  --output-dir D:\path\to\detector-audit
```

It writes `translation_change_scores.csv` and
`translation_change_summary.json`, including the observed frame cadence,
registration-status counts, score distribution, replay proposal times, and a
descriptive comparison with legacy candidates. That comparison is an audit,
not accuracy, because legacy candidates are not human truth.

## Code map

| File | Role |
|---|---|
| `gui/auto_capture.py` | Translation registration, residual score, and live temporal gates |
| `gui/growth_app.py` | Production wiring and operator status text |
| `gui/growth_logger.py` | Event/context diagnostics and immutable capture provenance |
| `scripts/replay_translation_change_detector.py` | Read-only ZIP replay and audit outputs |
| `tests/test_auto_capture.py` | Synthetic invariance/fail-closed tests |
| `tests/test_growth_logger.py` | Persisted detector-diagnostic tests |

The older `scripts/rheed_change_detector.py` and
`scripts/rheed_change_detector_report.py` remain legacy pixel-difference
baselines. They do not describe the current production detector.
