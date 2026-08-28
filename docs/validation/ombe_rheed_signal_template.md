# O-MBE RHEED Signal Validation

## Run Identity

- Date/operator:
- Analysis commit:
- WGC capture commit(s):
- Workstation/camera/kSA versions:
- Windows/DPI:
- Confirmed bare STO sample/run:
- Temperature, pressure, exposure, ramp and dwell context:
- External raw directories:
- Input SHA-256 manifest:
- Classifier checkpoint path/SHA-256 (if used):

## Safety and Input Gate

This workflow is passive and offline. Do not change growth conditions solely
to create a detector event. Classifier results are allowed only on confirmed
bare STO and are not FeSe-film quality measurements.

Generate each condition from `codex/wgc-rheed-live-test` with an interval of
1 s and `--save-every 1`. The analyzer rejects sparse runs, missing atomic
capture metadata, non-WGC inputs, or mismatched raw/cropped hashes.

| Condition | Description | Independently stable? |
|---|---|---|
| stable | Fixed surface and exposure | yes/no |
| exposure | Natural/operator-intended exposure change | no |
| window | Move/cover/front-back actions | no |
| natural-change | Naturally occurring experiment change | no |

## Durable Analysis

Long classifier runs use the managed launcher:

```powershell
$job = 'C:\O-MBE-validation\jobs\rheed-signal-run1'
$out = 'D:\lab-data\summaries\rheed-signal-run1'
& 'C:\Users\Yao_Yufan\.codex\skills\managed-long-jobs\scripts\managed_job.cmd' launch --output-dir $job --cwd $PWD -- python scripts\analyze_ombe_rheed_signals.py --input stable=D:\lab-data\run1\stable --input exposure=D:\lab-data\run1\exposure --stable-condition stable --output-dir $out
& 'C:\Users\Yao_Yufan\.codex\skills\managed-long-jobs\scripts\managed_job.cmd' status --output-dir $job
```

Poll status about every 30 seconds. For confirmed bare STO only, append
`--run-classifier --surface bare-sto --classifier-root D:\AI4MBE\RHEEDClassify`.

## Results

The analyzer reports both naive score crossings and events from an exact replay
of the current GUI policy: initial/adaptive warmup, rolling baseline,
three-frame debounce, and capture-time cooldown.

| Condition | BT.601 | Green | Specular ROI | Score p99/max | GUI events |
|---|---:|---:|---:|---:|---:|
| stable | | | | | |
| exposure | | | | | |
| window | | | | | |
| natural-change | | | | | |

- Duplicate/reversed WGC sequences and capture-gap distribution:
- Stable-run GUI false positives:
- Naturally labeled event recall:
- Specular position/ROI stability:
- Classifier quality/OOD proxy, flips, and successive-score drift:
- Diagnostic noise-floor threshold:

The generated threshold is a diagnostic suggestion only. Do not change the
production default until full-policy replay on independently labeled natural
changes demonstrates acceptable recall.

## Recommendation

- Preferred signal/ROI:
- Threshold evidence:
- Known confounders:
- Further data required:
