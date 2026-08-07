# RHEED ROI Intensity Monitor

Open **RHEED Trend** from the Monitor tab. The floating window displays the
current RHEED frame and plots the summed intensity inside a fixed region.

## Select a Region

- **Select rectangle**: drag directly on the displayed frame. The graphics
  scene maps the selection back to source-frame pixels, including when the
  preview is scaled or letterboxed.
- **Detect 1×1 triplet**: run the Equalizer three-bright-spot detector on one
  frozen frame. Inspect the three orange boxes, SNR, and confidence, then click
  **Use detected ROI**. Detection proposes geometry only; it does not prove
  that the surface is 1×1.
- **Spot box** controls each proposed box width in the detector's 128×96
  coordinate frame. Changing it does not modify an already accepted ROI.

Automatic boxes are fixed after confirmation. They are not re-detected on
each frame, because a moving ROI would mix tracking motion into the intensity
trend. The plotted metric is the union of the three boxes, so overlapping
pixels are counted once.

## Validity and Logging

An ROI is bound to the capture backend, HWND, crop/DPI geometry, and exact
frame dimensions. Disconnecting the camera or changing any of these values
invalidates the ROI and stops measurements until the grower selects it again.

During an active session:

- `rheed_roi_intensity.csv` stores one row per unique capture sequence,
  including sum, mean, pixel count, frame age, and capture provenance.
- `rheed_roi_definitions.jsonl` records ROI creation, replacement, clearing,
  and invalidation.
- `temporal_trace.jsonl` records measurement timing for latency analysis.

The metric is `bt601_display_luminance_sum_v1`. With WGC it represents the
8-bit false-colour image rendered by kSA, not raw detector counts. Keep camera
exposure, gain, kSA LUT, and display processing fixed when comparing values.
The current camera worker samples at approximately 1 Hz, so this monitor is
intended for slow trends rather than high-frequency RHEED oscillations.
