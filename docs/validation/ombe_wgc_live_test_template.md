# O-MBE WGC Live Test

## Run Identity

- Date/operator:
- Git commit:
- Workstation: Bulbasaur
- Windows version:
- Display DPI/scaling:
- kSA version and Live Video title:
- Python and `windows-capture-interpreter` versions:
- Raw artifact directory:
- SHA-256 manifest:

## Configuration

- Camera mode: `screengrab`
- Worker interval: 1 s
- WGC first-frame/stale timeouts: 5 s / 5 s
- Crop: top 75 px, bottom 30 px
- Probe command:
- Managed-job status directory:

## Checklist

- [ ] Detached top-level Live Video accepted; MDI child rejected.
- [ ] Covering Live Video does not contaminate captured frames.
- [ ] Moving it and changing foreground/background does not change the source.
- [ ] Crop remains correct at the active DPI.
- [ ] Minimize clears the GUI frame and stops heartbeat/auto-capture writes.
- [ ] Closing Live Video produces a clear disconnected error.
- [ ] Restoring Live Video and reconnecting yields a fresh timestamp.
- [ ] First post-reconnect sequence is greater than the last pre-loss sequence.
- [ ] One-hour probe completes without a stale gap or orphan capture thread.
- [ ] WGC and unobstructed MSS frames have equivalent RHEED content/crop.
- [ ] Signal-analysis corpus was separately captured with `--save-every 1`.

## Summary Metrics

| Metric | Result |
|---|---|
| Reads / duration | |
| Sequence stalls | |
| Frame age median / p95 / p99 / max | |
| RSS start / end / growth | |
| Python threads start / end | |
| Native OS threads start / end | |
| Heartbeat files after failure | |

## Evidence and Findings

List only small representative images here. Keep the complete corpus outside
Git and identify it with the manifest above.

- Outcome:
- Deviations:
- Follow-up:
