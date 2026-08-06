# RHEED Change Detector — Algorithm and Validation

**Status:** Draft for group review · Last updated 2026-04-28

> **Production update (2026-08-06).** The live GUI now uses
> `ReconstructionChangeCaptureEngine`. It learns a stable model-label
> baseline, requires three consecutive unique capture sequences before
> accepting a label change, and saves one full-resolution frame per second
> from 60 seconds before through 60 seconds after the first candidate frame.
> Bad/OOD output, camera discontinuity, model-version changes, and output-class
> changes reset or suspend detection. The output contract is dynamic: models
> with `1x1` retain it; models without it use only their actual labels. The
> pixel method below remains an offline diagnostic and compatibility baseline.

This document describes the historical algorithm behind the Growth Monitor's
RHEED auto-capture (`gui/auto_capture.py::PixelDiffChangeDetector` and
`AutoCaptureEngine`), the empirical evidence supporting its threshold and
mode choices, and the limitations a reader should be aware of when
interpreting its output.

It is the methods companion to `scripts/rheed_change_detector_report.py`,
which renders the validation evidence as a self-contained HTML report.

---

## 1. Problem statement

During an MBE growth, the RHEED pattern transitions through several distinct
surface reconstructions as conditions evolve (substrate temperature, deposition
rate, surface coverage). Identifying *when* these transitions happen is
valuable for two reasons:

1. **Data efficiency.** A growth produces tens of thousands of RHEED frames,
   but only ~30–50 are diagnostically interesting (Justin Meng's recommended
   training-set sweet spot). Saving every frame wastes disk and dilutes the
   signal in any downstream supervised task. Saving only at transitions
   keeps the dataset focused.
2. **Operator support.** Growers historically log entries by hand at
   transitions they observe. An automatic detector that prompts the grower
   at the right moments is a step toward the project's "AI making suggestions
   that the human follows with high success rate" milestone (Apr 14 group
   meeting).

The first version of the detector deliberately uses **classical pixel-domain
statistics, not a learned model**. Justification in §6.

---

## 2. Algorithm

### 2.1 Frame preprocessing

Each incoming frame is reduced to a single-channel `float32` grayscale image
on the [0, 255] intensity scale. For RGB inputs (the common case — kSA 400
ships RHEED frames as false-colored RGB screengrabs), the **green channel**
is taken directly rather than computing a luminance average. RHEED intensity
maps onto green in the kSA palette; mixing in red/blue dilutes the signal.

For single-channel inputs, PIL's `'L'` luminance conversion is used.

### 2.2 The two comparison modes

The detector computes a single scalar per frame: the **mean absolute pixel
difference** between the current frame and a reference. Two modes differ in
what they take as the reference.

**Mode A — `previous`:** reference is the immediately preceding frame.

```
score[i] = mean( | frame[i] - frame[i-1] | )
```

This catches the *rate* of change. Sharp transitions produce a single tall
spike; sustained transitions produce only a brief peak followed by a return
to baseline as both frames update together.

**Mode B — `buffer-mean`:** reference is the per-pixel mean of the last `N`
frames (default `N=20`, FIFO).

```
score[i] = mean( | frame[i] - mean(frame[i-N..i-1]) | )
```

This catches the *magnitude* of sustained shifts. A transition that lasts
several frames produces a tall plateau because the new frames are compared
against the older, stable reference.

The live GUI (`PixelDiffChangeDetector`) uses **buffer-mean** by default —
it gives more frames-per-event for downstream context, and its peaks are
larger (better signal-to-noise headroom over baseline).

### 2.3 Smoothing

The raw score timeseries is passed through a centered rolling mean
(`smooth_window`, default 3 frames). This removes single-frame spikes
caused by camera read noise or screengrab race conditions while preserving
real transitions that span multiple frames.

### 2.4 Triggering: threshold + debounce + cooldown

`AutoCaptureEngine` wraps the detector with three additional gates:

1. **Threshold.** The smoothed score must exceed a fixed value (default 2.0)
   for a frame to be considered "above noise." See §3 for the rationale.
2. **Debounce.** The threshold must be exceeded on `debounce_required`
   consecutive frames (default 3). Single-frame outliers don't fire the
   trigger even if they squeak past the threshold.
3. **Cooldown.** After a successful fire, no further triggers are allowed
   for `cooldown_s` seconds (default 5.0). Prevents a single sustained
   transition from firing repeatedly while the smoothed score is still
   elevated.

A fourth gate, **warmup**, suppresses triggering for the first 30 frames
of a session. This lets the buffer fill before any score is even computed.

### 2.5 Implementation note: O(1) buffer-mean

Naively, computing the buffer mean each frame costs `O(buffer_size · pixels)`.
The implementation maintains a running per-pixel sum and updates it
incrementally:

```python
if len(self._buffer) == self._buffer_size:
    self._sum -= self._buffer[0]   # subtract the about-to-be-evicted frame
self._buffer.append(gray)
self._sum += gray
buffer_mean = self._sum / len(self._buffer)
```

Per-frame cost is `O(pixels)` regardless of buffer size, which matters at
the camera's native ~10 Hz frame rate.

---

## 3. Threshold rationale

The default threshold `2.0` was chosen by validating against a reference
dataset (Rahim's `2022_02_04` STO substrate trajectory, 388 chronologically-
ordered frames). On that dataset:

| Mode | Baseline mean ± std | Threshold position | Flagged frames | Discrete events |
|---|---|---|---|---|
| `previous` | 0.553 ± 0.222 | 6.5σ above noise | 10 (2.6%) | **3** |
| `buffer-mean` | 0.825 ± 0.327 | 3.6σ above noise | 44 (11.3%) | **3** |

Two pieces of evidence support the choice:

1. **Statistical headroom.** For the live mode (`buffer-mean`), the
   threshold sits at `0.825 + 3.6×0.327 ≈ 2.0` — more than 3σ above the
   noise floor, which by convention separates "this is a real signal"
   from "this could be a fluctuation."
2. **Cross-mode agreement.** Both modes independently flag the same
   number of discrete events (3) on the same data, despite very different
   sensitivity profiles (10 vs 44 flagged frames). When two algorithms
   with different bias/variance tradeoffs converge on the same answer,
   the answer is unlikely to be a detector artifact.

The full distribution of scores, the timeseries, the three flagged events
themselves, and a threshold sensitivity sweep are rendered in
`validation_report.html` produced by `scripts/rheed_change_detector_report.py`.

### Caveats

- **Single dataset.** All numbers above come from one trajectory. Threshold
  generalization to other materials, growth conditions, or kSA palette
  configurations has not been validated. Re-run the report on each new
  reference dataset before assuming `2.0` is correct.
- **Chronological ordering matters.** Lexicographic sort of these filenames
  scrambles temperature ramp order (165°C frames sort before 291°C frames
  even though they are from the cool-down phase). The report uses Rahim's
  rename log to recover true chronological order. Earlier informal numbers
  ("baseline ~0.5–1.0") were measured against scrambled order and were
  approximately correct only by coincidence.
- **Adaptive thresholding deferred.** A μ + Nσ threshold updated online
  would adapt to per-session noise levels. Worth implementing if the
  fixed threshold proves brittle across materials. Tracked in the v3
  pending threads.

---

## 4. Buffer size choice

`buffer_size = 20` corresponds to roughly 2 seconds of acquisition at the
nominal 10 Hz frame rate. Motivation:

- Too small (e.g., 5): the buffer mean tracks sustained transitions
  quickly enough that the score returns to baseline before the trigger
  has a chance to fire. Defeats the "magnitude of sustained shift"
  property that makes buffer-mean useful.
- Too large (e.g., 200): the reference becomes stale, capturing slow
  thermal drift as a "change" and inflating the baseline.
- 20 sits in the regime where reconstruction transitions (typically
  ~5–15 frames in this dataset) are fully resolved as plateaus while
  baseline drift stays low.

Not exhaustively tuned. A buffer-size sensitivity sweep would be a useful
addition to the validation report.

---

## 5. Cross-validation against grower observations

Validating that the 3 flagged events correspond to grower-observed
transitions is the next step and requires either (a) Rahim's session notes
from 2022-02-04, or (b) a fresh OMBE run where the grower marks transitions
in real-time and we cross-reference the auto-capture event timestamps.
Tracked under "lab visit checklist" in `v3_pending_threads`.

Until that cross-validation lands, the detector should be considered
**algorithmically validated** (it consistently flags large, non-random
changes in the pixel statistics) but not **semantically validated** (the
flags have not been confirmed to correspond to real reconstruction
transitions).

---

## 6. Why classical, not learned

The Apr 17 internal meeting decision was to ship the pixel-diff detector
*before* swapping in any learned model (e.g., Classifier2 confidence
change, SimCLR embedding distance). Three reasons:

1. **UX-first.** The novel object in this part of the system is the
   human-AI dialog (the "progress bar" prompt that asks the grower to
   confirm a save). Building that UX with a deterministic, easily-
   inspected detector means the grower's trust in the *interaction
   pattern* doesn't get entangled with their trust in the *model*.
   Once the dialog feels right, swapping in a smarter detector is
   a drop-in replacement against the same `ChangeDetector` ABC.
2. **Failure modes are legible.** When pixel-diff fires incorrectly,
   the cause is always inspectable: a brightness shift, a screen
   refresh artifact, a frame drop. When a learned model fires
   incorrectly, the cause requires a separate explainability step.
3. **Cross-material portability.** Pixel-diff makes no assumptions
   about what the RHEED pattern *should* look like. Learned models
   trained on STO will need re-training or transfer learning for
   FeSe and other materials. The pixel-diff baseline will work
   on any material from day one.

This is also the staging path the `auto_capture` module is designed for:
`IntensityChangeDetector` (Tier 1), `PixelDiffChangeDetector` (Tier 1.5,
current), `EmbeddingChangeDetector` (Tier 2, planned), and
`ClassificationChangeDetector` (Tier 3, scaffolded).

---

## 7. References to code

| File | Role |
|---|---|
| `gui/auto_capture.py` | Production model-transition engine plus legacy pixel detectors |
| `gui/growth_app.py` | Binds classifier outputs to camera frames and session lifecycle |
| `gui/growth_logger.py` | Schema and CSV writer for `auto_capture_events.csv` |
| `scripts/rheed_change_detector.py` | Offline detector script — CSV + 1D plot, useful for quick threshold tuning |
| `scripts/rheed_change_detector_report.py` | Offline detector report — full validation HTML |

## 8. Open items (tracked elsewhere)

- Buffer-dump on flagged event (save the full ring buffer for pre-event context)
- Banner UI with countdown (the "progress bar" prompt from the Apr 17 design)
- Manual pause / emergency stop button
- Cross-reference event flags against grower-observed transitions
- Buffer-size sensitivity sweep
- Adaptive (μ + Nσ) threshold

See `v3_pending_threads.md` (memory) for the active workstream tracker.
