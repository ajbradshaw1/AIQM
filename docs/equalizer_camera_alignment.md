# Equalizer Camera Alignment

The Equalizer compares a frozen kSA RHEED frame with simulator images in a
canonical 128 x 96 coordinate system. Camera alignment is a geometric and
provenance gate; it does not change classifier output or assert surface
quality.

## Safety and Provenance

- Claude prototype snapshot: `3b7f5fc`, preserved locally as
  `codex/equalizer-camera-alignment-claude-snapshot`.
- Original QC stash object: `4a8e24405d8a67048d12f3f82053a68dc23e336f`.
- Original pre-WGC stash object: `d722f5103dce115741700e4c2c0c746f410f0af6`.
- Untracked QC-test safety stash object:
  `303274ad48ba4ef7d538f179fe44927a675eb670`.

Do not drop these stash objects until the alignment and QC branches are pushed
and the Bulbasaur reports are complete.

Every accepted calibration is appended to
`equalizer_calibrations.jsonl`. Live and retrospective labels reference its
`calibration_id`, basis bundle, view segment, visual-history generation, and
the exact captured frame. `GrowthApp`, not an Equalizer tab, owns the accepted
record and its lifecycle. A calibration is invalid after a session boundary,
gun realignment, camera disconnect/reconnect, backend or HWND change,
resolution/ROI/DPI change, visual-history generation change, or basis-bundle
change. These discontinuities also cancel a frozen candidate under review;
returning to the old geometry does not revive it.

## Grower Workflow

1. Start a session and confirm the physical RHEED gun alignment.
2. Open **Live Equalizer** and select **Calibrate**. The calibration view is a
   copied frame; the WGC worker continues acquiring in the background.
3. Use automatically detected 1x1 points or click left, centre, and right
   points manually.
4. Review both normal and mirrored candidates. Green is the frozen live frame;
   magenta is the warped basis selected in **Overlay basis**. Switch among
   `1x1`, `Tw(2x1)`, `c(6x2)`, and `RT13`; inspect landmarks, residuals,
   rotation, scale, coverage, and parity.
5. In **Handedness evidence**, select the naturally present asymmetric
   reconstruction or **Clear live streak / tail**. Check the explicit
   confirmation box only after the selected candidate agrees with that
   evidence. The **Request acceptance** button remains disabled otherwise;
   three nearly symmetric 1x1 points alone never prove handedness.
6. Auto-fit or adjust the four active sliders, then save. The label operation
   uses another immutable image-plus-provenance snapshot, so a new callback
   cannot mix one image with another frame's timestamp or sequence. HTR is deliberately
   unavailable until a view-provenanced canonical HTR basis is rebuilt; its
   CSV value remains blank rather than falsely recording zero.

The Events Equalizer uses the calibration associated with its selected frame.
If that frame has no compatible calibration, it must be calibrated explicitly;
raw simulator images are never used silently.

The application hashes and saves the immutable calibration snapshot when it
accepts a candidate. The journal records the evidence kind, evidence image
path, and SHA-256 together; partial evidence metadata is rejected. The first
accepted record for each basis ID also embeds one complete bundle manifest
(version, canonical coordinate frame, source paths, identity transforms, and
per-asset SHA-256 values). Later records reference that same content-addressed
ID. Capture timestamps must be timezone-qualified ISO-8601 UTC values.

Live label writes use a durable pending transaction marker, a staged image,
and atomic CSV replacement. Calibration acceptance likewise writes its WAL
before the evidence PNG and journal line. Auto-capture writes one WAL before
any context image, then commits the exact `auto_capture_events.csv` row and
buffer manifest as a unit. On restart, a committed row keeps or finishes its
verified buffer; an uncommitted transaction is rolled back. Events labels only
reference an existing selected frame under the session `frames/` tree and
never create a fallback image. Malformed or conflicting recovery state remains
fail-closed for operator inspection. Growth Monitor applies and scans the
configured workstation log root before START; unresolved recovery blocks a new
session instead of being missed under the constructor default.

`scripts/equalizer_ui.py` is an import-only helper module for canonical basis
loading, display palettes, and fitting math. Its legacy window cannot save and
direct execution exits nonzero with instructions to use Growth Monitor. Do not
restore a standalone CSV labeling path because it bypasses calibration and QC.

## Offline Gate

Run with the workstation GUI environment:

```powershell
$python = 'D:\Environment_Cache\conda_envs\ai4mbe-gui\python.exe'
& $python -m pytest -q tests/test_equalizer_alignment.py
& $python -m pytest -q tests/test_live_equalizer_tab.py
& $python -m pytest -q tests/test_equalizer_app_integration.py
& $python -m pytest -q tests/test_events_equalizer_alignment.py
& $python -m pytest -q tests/test_growth_logger.py
& $python -m pytest -q tests/test_classifier_worker.py
& $python -m pytest -q tests/test_rheed_qc_flow.py
& $python -m pytest -q tests/test_rheed_qc_export_integration.py
& $python -m pytest -q tests/test_worker_shutdown.py
& $python -m pytest -q tests/test_movie_export.py
& $python -m pytest -q tests/test_screengrab_camera.py
& $python -m pytest -q tests/test_window_capture.py
& $python scripts/simulate_alignment.py
git diff --check
```

Any printed validation failure must return a non-zero process exit code.
`scripts/test_wgc_capture.py` is the separate Bulbasaur live-window probe; run
it with the duration/output arguments in `docs/bulbasaur_smoke_test.md`, not
as a `unittest` import.

## Bulbasaur Gate

- Verify automatic and manual landmark paths, both parity previews, all four
  overlay-basis choices, and the mandatory evidence confirmation gate.
- Commission handedness with a naturally occurring asymmetric
  Tw/c(6x2)/RT13 or streak/tail frame; record the evidence SHA-256. Historical
  Vimba/BGW files do not establish the current kSA/WGC orientation. Capture a
  new WGC frame and direct Vimba uint16 frame sequentially on a stable surface,
  record their time separation and kSA display transform, and do not describe
  the pair as simultaneous. The direct-camera probe must pass strict
  trigger-to-callback-to-save accounting before its frame is accepted.
- Exercise WGC under occlusion, movement, foreground/background changes,
  resize/DPI changes, minimize, close, and explicit reconnect.
- Confirm failure clears the frame, invalidates calibration, and prevents new
  labels; a new session or restored camera requires fresh acceptance.
- Close the GUI unexpectedly and verify parsable CSV/JSONL output and no
  classifier/capture worker left alive.
- Run the one-hour WGC stability probe through `managed-long-jobs`; keep full
  frames outside Git and commit only summaries, hashes, and representative
  overlays.

Instrument power loss and serial/PC disconnect remain in
`codex/ombe-read-failure-validation`; they are a final system gate, not an
Equalizer-algorithm test.

Record commissioning evidence and results with
`docs/validation/ombe_equalizer_alignment_template.md`.
