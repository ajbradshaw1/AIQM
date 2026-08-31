# RHEED ROI Intensity Monitor

Open **RHEED Trend** from the Monitor tab. The floating window displays the
current RHEED frame and plots the summed intensity inside one or more fixed
regions.

## Select Regions

- **Select rectangle**: drag directly on the displayed frame, *replacing*
  whatever is tracked now. The graphics scene maps the selection back to
  source-frame pixels, including when the preview is scaled or letterboxed.
- **Add rectangle**: drag another box *alongside* the current ones, up to
  eight. Each box is drawn in its own colour, named on the frame, and plotted
  as its own curve — so the specular spot can be watched independently of the
  first-order streaks, or against a background box.
- **Detect 1×1 triplet**: run the Equalizer three-bright-spot detector on one
  frozen frame. Inspect the three orange boxes, SNR, and confidence, then click
  **Use detected ROI**. Detection proposes geometry only; it does not prove
  that the surface is 1×1. The three boxes are named `left`, `specular` and
  `right`, in the detector's left-to-right order. This path validates the
  left/specular/right ordering and rejects collinear or too-close triples.
- **Detect spots** (with **Spots: N**): place a box on each of the N brightest
  separated maxima, named `spot 1`…`spot N` in descending brightness. It makes
  **no claim about the diffraction pattern** — unlike the triplet detector it
  applies no geometry validation, and it accepts vertically stacked spots.
  Use it when you want several regions tracked and do not need the named 1×1
  geometry. Also requires **Use detected ROI** to confirm.
- **Remove last box**: drops the most recently added box only. Deliberately
  not "remove any box": the per-region log is keyed by label, so removing a
  middle box would renumber the ones after it and splice two different
  rectangles into one trace.
- **Fit to spot** (on by default) sizes each proposed box to that spot's own
  measured width and height — see *Box shape* below. Turn it off to get the
  fixed square set by **Fixed box** instead.
- **Fixed box** controls the fallback box width in the detector's 128×96
  coordinate frame, used when fitting is off or measurement fails. Changing
  it does not modify an already accepted ROI.

Boxes are fixed after confirmation. They are not re-detected on each frame,
because a moving ROI would mix tracking motion into the intensity trend.

## Box Shape

**RHEED features are not round, so the boxes are rectangles.** First-order
features are streaks elongated along y; the specular spot has its own shape.
On a lab STO frame the first-order spots measure about 7×19 px against a
34×48 px specular — aspect ratios of ~2.5 and ~1.4 in the *same image*, so
neither one square nor one global aspect ratio is right.

With **Fit to spot**, each box is sized independently per axis from that
spot's own full-width-at-half-maximum, times `SPOT_BOX_FWHM_MULTIPLE` (1.5).
For a Gaussian that captures ~92% of the profile on each axis while keeping
background out — background adds a constant offset that dilutes the
fractional change the trend exists to show.

The measurement is made at **source resolution**, not in the detector's
128×96 frame. That frame is right for *finding* spots but useless for
measuring them: one processed pixel is about five source pixels, so a 7-px
streak is barely two pixels across and its centre is quantised to the same
grid. The peak is re-located in the source frame first.

Fitted boxes are floored at 6 px and capped at 35% of the frame, so neither
sub-pixel drift nor a saturated bloom can produce an unusable region. If
half-maximum measurement fails for a frame, the whole proposal falls back to
fixed squares rather than mixing fitted and fixed boxes — a mix would make
the per-box sums incomparable for a reason nothing on screen explains. The
`detector_method` recorded with the ROI ends in `-fitted` or `-fixed`.

Manually drawn boxes have always been arbitrary rectangles; this applies to
the automatic proposals.

## What Is Plotted

- One curve per box, when there are two or more. A single box plots only the
  union, because a second identical line would imply a distinction that is not
  there.
- **all boxes (union)**, always. Overlapping pixels are counted once here.
  Each *per-box* curve counts its own pixels in full — "what is this box
  doing" is a different question from "how much unique area is lit".
- **Normalise (Δ%)** replots every series as percent change from its own
  baseline. Boxes of very different brightness compress the dim ones against
  the axis in absolute units; normalised, their *shapes* compare directly.
  The percentages are recorded at measurement time against the baseline in
  force then, so toggling the view never rewrites history — including across
  an exposure change, which re-seeds the baselines.

## Validity and Logging

An ROI is bound to the capture backend, HWND, crop/DPI geometry, and exact
frame dimensions. Disconnecting the camera or changing any of these values
invalidates the ROI and stops measurements until the grower selects it again.

Adding or removing a box mints a new ROI definition ID rather than editing the
old one, so samples already logged stay attributable to the geometry they were
measured with. Labels carry across the change: appending a box to the detected
triplet leaves `specular` still called `specular`.

During an active session:

- `rheed_roi_intensity.csv` — one row per unique capture sequence, holding the
  **union** sum, mean, pixel count, frame age, exposure, and capture
  provenance. Schema 2 appended `exposure_us` and `exposure_generation` as
  trailing columns.
- `rheed_roi_region_intensity.csv` — one row per **box** per capture, keyed by
  `roi_definition_id` + `region_label` + `capture_sequence`.
- `rheed_roi_definitions.jsonl` — ROI creation, replacement, clearing,
  invalidation, and exposure changes.
- `camera_exposure_changes.jsonl` — every live exposure change, confirmed or
  refused, with the previous and confirmed values.
- `temporal_trace.jsonl` — measurement timing for latency analysis.

## Exposure

Camera exposure is changeable live in direct Vimba mode: set **Direct
exposure** in the Session tab and press **Apply**. No DISARM/ARM cycle, and no
interruption to the session.

Because the metric is a display-luminance sum, an exposure change scales it
with nothing having happened on the sample surface. The monitor therefore:

- breaks every curve at the change, so no line is drawn across it,
- marks the position with a dashed vertical line,
- re-seeds the relative-change baselines against the new exposure, and
- records the change in both journals above.

**Values either side of a break are not comparable.** Gain, the kSA LUT, and
display processing are not tracked this way — keep those fixed.

Live exposure requires **Full** camera access. If kSA or the Vimba X Viewer
holds the camera the driver negotiates Read access, and the request is refused
with that reason rather than silently ignored.

## Metric

`bt601_display_luminance_sum_v1`. With WGC it represents the 8-bit false-colour
image rendered by kSA, not raw detector counts. The camera worker samples at
approximately 1 Hz, so this monitor is intended for slow trends rather than
high-frequency RHEED oscillations.
