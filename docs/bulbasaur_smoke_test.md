# Bulbasaur WGC Smoke Test

This procedure validates only read-only RHEED capture on the O-MBE
workstation. Do not change instrument setpoints, unplug interfaces, or power
down equipment. Keep complete images and long logs under `logs/`, outside Git.

## Install and Launch

```powershell
git fetch origin
git switch codex/wgc-rheed-live-test
.\.venv\Scripts\python.exe -m pip install -r requirements-windows-live.txt
.\.venv\Scripts\python.exe -m pip show windows-capture-interpreter
.\.venv\Scripts\python.exe growth_monitor_app.py
```

The package version must be exactly `1.5.1`. In kSA 400, detach **Live
Video** into its own top-level window. Select camera mode `screengrab`; use
`screengrab_mss` only for an explicit legacy diagnostic. WGC never silently
falls back to MSS.

## Interactive Checks

- ARM and confirm RHEED frames update with backend `wgc`.
- Cover Live Video completely, move it, and change foreground/background.
  The covering window must never appear in captured frames.
- Verify the 75 px top / 30 px bottom crop at the active Windows DPI.
- Minimize Live Video. Within one camera-worker cycle the displayed frame,
  classifier source, heartbeat, Live Equalizer save, and auto-capture image
  writes must stop.
- Restore the window and click **Reconnect RHEED**. A fresh timestamp must
  arrive, and its sequence must exceed the last pre-failure sequence.
- Repeat by closing and reopening Live Video.

## Evidence Probe and One-Hour Run

Use the durable launcher because these commands exceed ten seconds:

```powershell
$job = 'C:\O-MBE-validation\jobs\wgc-one-hour'
$raw = 'C:\O-MBE-validation\raw\wgc-one-hour'
& 'C:\Users\Yao_Yufan\.codex\skills\managed-long-jobs\scripts\managed_job.cmd' launch --output-dir $job --cwd $PWD -- .\.venv\Scripts\python.exe scripts\test_wgc_capture.py --duration-s 3600 --interval 1 --save-every 10 --output-dir $raw
& 'C:\Users\Yao_Yufan\.codex\skills\managed-long-jobs\scripts\managed_job.cmd' status --output-dir $job
```

Poll status about every 30 seconds. The probe always writes `summary.json`,
`capture_metadata.json`, and `sha256_manifest.json`, including on capture
failure or operator interruption. It records native OS thread counts so Rust
WGC threads are included.

For an unobstructed WGC/MSS comparison, add `--compare-mss`. For the offline
RHEED signal branch, run a separate corpus with `--save-every 1`; sparse
one-in-ten images do not represent the GUI detector cadence.

Record results in
`docs/validation/ombe_wgc_live_test_template.md`. Commit only the completed
Markdown report, small summaries, the hash manifest, and a few representative
crops.
