# O-MBE Fault, Disconnect, and Recovery Validation

## Run Identity

- Date, operator, observer, owner/approver:
- Campaign, scenario, and episode IDs:
- Recorder/software commits and dirty state: see `run_info.json`
- Windows version, workstation, DPI, vendor software, driver, and firmware:
- Exact target and physical/logical boundary:
- Authorization and SOP references:
- Raw run/job directories and verified SHA-256 manifest:

## Safety Boundary

The probe records observations and operator markers. It sends no
state-changing command, does not close vendor software, disable a NIC/COM
device, operate a relay/PDU, change a setpoint, or switch power. Direct
Exactus, Modbus, and JSON-RPC modes do issue normal read requests. The local
network watcher uses `netsh` link state and sends no probe packet.

A marker records an approved manual action; it never authorizes or performs
one. Every S1-S4 run requires named roles, an approved safe state, an SOP,
written abort criteria, `--confirm-safe-state`, and a `precheck-complete`
marker. Stopping a file writer or closing/minimizing vendor software can affect
control and is not assumed safe merely because it is classified S1.

Never induce S2/S3 during growth, heating/ramping, autonomous control, or when
the target participates in control, interlocks, vacuum, pumping, plasma,
source output, shutters, or safety monitoring. Never deliberately power-cycle
the RHEED gun/HV, effusion cells, heaters, pumps, vacuum/plasma/PLC
controllers, shared racks/switches, or an active Bulbasaur workstation.

## Safety Tiers

| Tier | Fault class | Allowed execution |
|---|---|---|
| S0 | `offline-injection` | Unit-test harness only; live `run` rejects this label |
| S1 | `window-source-loss`, `file-feed-stop` | Approved manual software/display/feed action |
| S2 | `transport-loss` | Approved manual interruption of one dedicated telemetry link |
| S3 | `instrument-power-loss` | Vendor-approved non-control-critical device/adapter only |
| S4 | `workstation-power-observation` | Tabletop or planned maintenance with independent evidence |

An ineligible scenario is recorded as **not tested**, not forced into the
matrix. S4 normally uses a second computer, UPS, or Windows event evidence; do
not intentionally hard-cut a workstation that controls equipment.

## Preflight

- [ ] No growth, ramp, PID/autonomous action, or sample at risk.
- [ ] Outputs are safe and shutters are closed where applicable.
- [ ] Pressure, vacuum, cooling, and interlocks are confirmed safe.
- [ ] The exact cable/device/window/feed boundary is identified.
- [ ] The owner confirms it is not a control or safety path.
- [ ] Operator, observer, approver, authorization, and SOP are recorded.
- [ ] Protocol, COM/baud/device ID, NIC index, and hardware identity are known.
- [ ] Abort criteria and vendor recovery procedure are immediately available.
- [ ] Growth Monitor is advisory/disarmed and cannot auto-resume.

The CLI fails closed when required metadata is missing. For serial/USB tests,
the `failure-start` marker also requires a recent watchdog sample with the
exact COM configuration and a device HWID/serial number. Network monitoring
records development evidence only; S2 Ethernet is disabled until the endpoint
and driver are bound. Every S1-S4 `failure-start` also requires the
recorder's kernel liveness lease to be active, the latest State and watchdog
samples to be fresh and on the current boot, and a healthy trailing baseline
for every relevant channel. A completed `summary.json` blocks a new failure
start.

## Durable Recorder

Use new raw and job directories. Growth Monitor remains interactive and is not
launched by the job manager.

```powershell
$manager = "$env:USERPROFILE\.codex\skills\managed-long-jobs\scripts\managed_job.cmd"
$python = "C:\Users\Yao_Yufan\.conda\envs\ai4mbe-gui\python.exe"
$checkout = "D:\path\to\failure-validation-checkout"
$run = "D:\O-MBE-validation\failure\serial_link_YYYYMMDD_HHMMSS"
$job = "D:\O-MBE-validation\jobs\serial_link_YYYYMMDD_HHMMSS"

& $manager launch --output-dir $job --cwd $checkout -- $python `
  scripts\ombe_failure_probe.py run `
  --source pyrometer --mode exactus --port COM4 --baudrate 115200 `
  --fault-class transport-loss --link-kind usb `
  --campaign-id OMBE-FAULT-001 --scenario-id exactus-link-loss-01 `
  --target "Exactus read-only pyrometer interface" `
  --boundary "dedicated PC-side USB/RS-422 adapter for COM4" `
  --operator "<name>" --observer "<name>" --approver "<owner>" `
  --authorization-ref "<maintenance record>" `
  --sop-ref "<disconnect/recovery SOP>" `
  --safe-state-note "<verified idle/advisory state>" `
  --abort-criteria "<alarms, interlock, output, wrong cable>" `
  --confirm-safe-state --duration-s 600 --baseline-duration-s 30 `
  --output-dir $run
```

This only starts observation. Exactus and Modbus share a per-port Windows
mutex. Close TemperaSure and do not run another serial reader. Non-default
Modbus COM/baud/device-ID arguments are rejected before a port is opened
because the b24ceb1 worker does not forward them. If a worker cannot stop, its
mutex remains held until the recorder process exits.

Poll the durable job at least every 30 seconds:

```powershell
& $manager status --output-dir $job
```

## Manual Marker Sequence

After starting the recorder, wait at least `--baseline-duration-s` and confirm
that the latest State and watchdog records are valid before any physical or
software fault action. The CLI rejects a baseline that is short, stale, on an
old boot, or unhealthy. Use one episode ID and one fault episode per run:

```powershell
& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind precheck-complete `
  --episode-id exactus-link-01 --label precheck

& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind failure-start `
  --episode-id exactus-link-01 --label link-removal-start

# The authorized operator performs the SOP action; the script does nothing.

& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind failure-observed `
  --episode-id exactus-link-01 --label expected-failure-visible

& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind recovery `
  --episode-id exactus-link-01 --label link-restored

& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind vendor-ready `
  --episode-id exactus-link-01 --label vendor-ready

& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind gui-reconnected `
  --episode-id exactus-link-01 --label gui-reconnected

& $python "$checkout\scripts\ombe_failure_probe.py" mark `
  --output-dir $run --kind rearmed `
  --episode-id exactus-link-01 --label explicit-rearm
```

The CLI enforces precheck, terminal-marker, and recovery-action order. If
recovery cannot be completed, terminate the episode with `abort`,
`persistent-failure`, or `incomplete`; these outcomes remain non-successful.
No fault episode, an unmatched start, or a missing re-ARM also returns nonzero.
Exactly one `failure-start` is allowed in each run directory. `rearmed` is an
operator/observer assertion: the probe does not inspect the Growth Monitor ARM
state, so verify it visually or with independent evidence.

## Network Boundary

List local IPv4 interface indices without contacting an instrument:

```powershell
netsh interface ipv4 show interfaces
```

The passive development watcher accepts `--network-interface-index <index>`
and optionally `--network-interface "<exact name>"`. It samples local adapter
state at `--network-probe-interval` (default 2 s) without opening a TCP
connection. The live command currently rejects S2 Ethernet/network loss
because no GUI driver is bound to a declared remote endpoint. Local NIC-down
is fault evidence only; it cannot prove that the selected instrument path
failed.

## Currently Enforceable Disconnect Matrix

| Scenario | Recorder support |
|---|---|
| Exactus/Modbus dedicated COM/USB link loss | S2, with active-driver COM settings and HWID/serial identity |
| Telemetry-only Ethernet adapter loss | **Not evaluable** until NIC, remote endpoint, and active driver are bound |
| Camera/MISTRAL/EvapControl USB cable loss | Not yet evaluable: add a source-bound PnP identity probe first |
| Window, UIA, WGC, or Elog source loss | S1 software/source-path test only |
| Workstation/site outage | S4 observation/tabletop with independent recorder |

Do not substitute the presence of an unrelated COM port or NIC for the target
interface. Unsupported cases are recorded as **not tested**.

## Source-Specific Interpretation

| Source | Candidate boundary | Recovery evidence and limitation |
|---|---|---|
| WGC/kSA | Vendor window; hardware link requires a future identity probe | WGC sequence proves capture delivery, not a new exposure; hardware recovery needs a vendor exposure counter or independent evidence |
| MISTRAL | Window only; Ethernet awaits endpoint/driver binding | JSON-RPC all-`None` is not evaluable; OCR confirms only the display path |
| EvapControl | Elog/display path; controller links are normally ineligible | Recovery requires advancing Elog source timestamps; mtime-only changes do not count |
| TemperaSure | Window/UIA path; COM loss requires a future identity probe | UI/OCR can retain stale display values and cannot prove hardware recovery |
| Exactus/Modbus | Dedicated USB/RS-422 link | Consecutive valid direct-read cycles confirm the read path, not instrument safe state |

The camera fingerprint is a sampled freeze candidate only: a static surface
may genuinely look unchanged, and a vendor window may repaint its last image.
For Elog, `watchdog.jsonl` records path, size, mtime, last-record source time,
and tail hash, but only source-time advancement confirms new records.

## Vendor GUI Unexpected Closure

Treat closing a vendor data window and exiting the complete vendor application
as different tests. A title-bar X or vendor Exit action is performed manually
by the named operator after `failure-start`; the recorder or process observer
must never send `WM_CLOSE`, call a close/kill API, or terminate a process.
Closing an application that owns control, interlock, or safety functions is
ineligible even when its readout is also used by Growth Monitor.

Before the action, bind the exact top-level data HWND to its owning PID,
process creation time, executable/command line, window title, and source
driver HWND. Do not accept a title-only match, a launcher/wrapper PID, the
recorder PID, or an unrelated `python.exe`. Record these outcomes separately:

- the target data HWND disappears while the vendor process remains alive;
- the vendor process exits, confirmed through its pre-opened process handle;
- another same-title window appears but is not yet bound as recovery.

An external window/process loss is fault evidence only. Passing also requires
the affected production State to become invalid by the stale deadline and the
actual Growth Monitor path to stop using the previous value. Check its session
logs and UI, not only the independent probe: affected sensor columns must not
repeat stale-valid values, and a lost RHEED source must not produce heartbeat
frames, event images, or new classifications from the cached frame. Reopening
the vendor software must bind the expected new HWND/process identity before
manual reconnect and re-ARM.

## Growth Monitor GUI Lifecycle and Unexpected Exit

The source probe runs its own workers and cannot establish whether the Growth
Monitor process is alive. A Growth Monitor lifecycle run therefore requires a
separate, durable, read-only process/window observer. The observer records its
own PID and binds the Growth Monitor main HWND to the actual Python process,
script command line, process creation time, and Git commit. It may query and
wait on the process but must have no terminate right and no close/kill command.
Growth Monitor remains an interactively launched application.

First test a graceful title-bar X with Growth Monitor idle/disarmed. A
RUNNING-path test is allowed only as an approved advisory test session while
the equipment itself is idle and non-controlling. Run both variants: no
auxiliary windows open, and RHEED/temperature/Equalizer or other floating
windows open. Distinguish:

- **main HWND lost:** the primary window disappears;
- **process exited:** the bound process handle signals and yields an exit code;
- **floating-window residue:** the main HWND is gone but the process or another
  top-level window owned by the same PID remains.

Main-window loss is not proof of process exit. If a floating window keeps the
process alive, verify that every worker, classifier, heartbeat, and session
write has stopped and that no stale display still appears live. Record a
lingering process, worker, or live-looking stale window as a validation
failure, not as successful shutdown.

Exercise abrupt termination first with a synthetic/offline process or a
tabletop copy. Do not automate it and do not proceed to a connected production
workstation until the offline evidence, safe-state review, and independent
observer have passed. A target crash is the planned fault; loss of the
independent observer is an evidence-fatal result.

Also keep a `--fault-style spontaneous` observer recipe ready for a genuinely
unplanned GUI disappearance. Record `precheck-complete` only while the healthy
baseline is still present, and do not add `close-action` afterward. Never
manufacture a spontaneous live crash. A loss before the required baseline or
without surviving observer evidence is incomplete, not a pass.

After either exit path, audit every existing session CSV/JSON for complete,
parseable records and observe the old session directory until it is quiescent.
Growth Logger calls Python `flush()` after common row writes and closes files
on a graceful X, but it does not `fsync()` those session files. This supports
process-exit testing only; it is not evidence of durability through
workstation or site-power loss.

On restart, require a new PID/process-creation identity and verify the GUI is
initially `idle`. It must not automatically ARM, START, resume control, or
append to the old session. The operator explicitly ARM(s), confirms fresh
source-appropriate readings, and then explicitly START(s) a new, cross-linked
test session. A `rearmed` marker alone remains an assertion unless accompanied
by read-only UI/state evidence.

### Read-Only Lifecycle Observer

Start Growth Monitor interactively. For a RUNNING-path test, use only an
approved advisory test session while the equipment is idle and no sample,
control loop, ramp, or autonomous action is active. Pin that exact session
directory; for an idle/disarmed test, omit the two session arguments.

```powershell
$growth = @(
  Get-Process |
    Where-Object MainWindowTitle -eq "Oxide MBE Growth Monitor"
)
if ($growth.Count -ne 1) { throw "Expected exactly one Growth Monitor" }
$growthMonitorPid = $growth[0].Id
$growthMonitorHwnd = $growth[0].MainWindowHandle
$growthMonitorImage = $growth[0].Path
if ($growthMonitorHwnd -eq 0) { throw "Growth Monitor has no main HWND" }
if (-not $growthMonitorImage) { throw "Cannot resolve Growth Monitor image" }
$session = "E:\OMBE\GrowthMonitor\<exact-test-session>"
$run = "D:\O-MBE-validation\failure\gui_exit_YYYYMMDD_HHMMSS"
$job = "D:\O-MBE-validation\jobs\gui_exit_YYYYMMDD_HHMMSS"
$precheckEvidenceFile = (
  "D:\O-MBE-validation\evidence\gui_precheck_idle_safe.png"
)
$closeEvidenceFile = (
  "D:\O-MBE-validation\evidence\gui_close_action_safe.png"
)

& $manager launch --output-dir $job --cwd $checkout -- $python `
  scripts\ombe_gui_lifecycle_probe.py run `
  --pid $growthMonitorPid `
  --hwnd $growthMonitorHwnd `
  --expected-title "Oxide MBE Growth Monitor" `
  --expected-image $growthMonitorImage `
  --expected-command-token growth_monitor `
  --target-role growth-monitor --scenario-id gui-running-x-01 `
  --fault-style graceful --test-context live-idle-advisory `
  --gui-state advisory-running `
  --session-dir $session --require-session-advance sensor_log.csv `
  --operator "<name>" --observer "<different name>" --approver "<owner>" `
  --authorization-ref "<maintenance record>" --sop-ref "<GUI-close SOP>" `
  --safe-state-note "<idle equipment; advisory test session only>" `
  --abort-criteria "<unexpected control, output, alarm, or wrong process>" `
  --confirm-safe-state --require-action-markers `
  --require-recovery-validation `
  --baseline-duration-s 10 --post-loss-observation-s 5 `
  --orphan-grace-s 30 --duration-s 300 --output-dir $run
```

The observer opens only `PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE`;
it has no close/terminate command. After the baseline is visibly healthy,
write markers from another terminal, then the named operator manually clicks
the main-window X. Repeat once with no floating windows and once with the
RHEED, temperature, and Equalizer windows open. The action gate rejects a
marker if sampling has ended, the latest sample/cadence is stale, the bound
window has already disappeared, a required stream is not advancing, or too
little observation time remains.

```powershell
& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" mark `
  --output-dir $run --kind precheck-complete `
  --label "baseline and safe state verified" --operator "<observer>" `
  --evidence-ref $precheckEvidenceFile

& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" mark `
  --output-dir $run --kind close-action `
  --label "operator about to click main-window X" --operator "<operator>" `
  --evidence-ref $closeEvidenceFile
```

Poll the managed job at least every 30 seconds. Before writing any recovery
marker, wait until status reports `result_available=true` and verify that
`$run\run_complete.json` exists. On restart, identify the exact replacement
process/window and let the marker command verify it read-only. Create the UI
evidence files before writing the corresponding marker; each reference must be
a regular file. The observer records its size, mtime, and SHA-256 and rechecks
it whenever the summary is rebuilt.

```powershell
$newGrowth = @(
  Get-Process |
    Where-Object MainWindowTitle -eq "Oxide MBE Growth Monitor"
)
if ($newGrowth.Count -ne 1) { throw "Expected one replacement GUI" }
$newGrowthMonitorPid = $newGrowth[0].Id
$newGrowthMonitorHwnd = $newGrowth[0].MainWindowHandle
$newGrowthMonitorImage = $newGrowth[0].Path
if ($newGrowthMonitorHwnd -eq 0) { throw "Replacement GUI has no main HWND" }
if (-not $newGrowthMonitorImage) { throw "Cannot resolve replacement image" }
$newSession = "E:\OMBE\GrowthMonitor\<new-linked-recovery-session>"
$idleEvidenceFile = "D:\O-MBE-validation\evidence\restart_idle.png"
$armedEvidenceFile = "D:\O-MBE-validation\evidence\restart_armed.png"
$newSessionEvidenceFile = (
  "D:\O-MBE-validation\evidence\restart_new_session.png"
)

& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" mark `
  --output-dir $run --kind gui-restarted `
  --label "replacement GUI identity verified" --operator "<observer>" `
  --new-pid $newGrowthMonitorPid `
  --new-hwnd $newGrowthMonitorHwnd `
  --new-expected-title "Oxide MBE Growth Monitor" `
  --new-expected-image $newGrowthMonitorImage `
  --new-expected-command-token growth_monitor

& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" mark `
  --output-dir $run --kind idle-confirmed `
  --label "replacement GUI starts idle" --operator "<observer>" `
  --evidence-ref $idleEvidenceFile

& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" mark `
  --output-dir $run --kind gui-rearmed `
  --label "operator explicitly armed replacement GUI" `
  --operator "<operator>" --evidence-ref $armedEvidenceFile

& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" mark `
  --output-dir $run --kind new-session-started `
  --label "operator explicitly started linked recovery session" `
  --operator "<operator>" --new-session-dir $newSession `
  --evidence-ref $newSessionEvidenceFile

& $python "$checkout\scripts\ombe_gui_lifecycle_probe.py" finalize `
  --output-dir $run --reaudit-session
```

For an idle test with no pinned session, omit `new-session-started` and
`--reaudit-session`, use `--gui-state idle-disarmed`, and omit all session
arguments; the other recovery markers still apply. For
`advisory-running`, the CLI requires a pinned session and at least one
`--require-session-advance`. Before `new-session-started`, wait until the new
session's `sensor_log.csv` has at least one complete data row. The run remains
exit 3 until the required recovery evidence is complete. Every recovery marker
reopens the stored read-only binding and verifies the new PID, process creation
time, executable, exact HWND/title/class, visibility, and command token; a
one-time restart assertion is insufficient.

Any post-exit or post-restart change to the old session is a validation
failure. The observer's exit code covers lifecycle evidence only; pair vendor
window tests with `ombe_failure_probe.py` to evaluate stale State, cached
frames, classification, and production logging.

For a vendor GUI test, first start `ombe_failure_probe.py` with
`--fault-class window-source-loss`. Then start the lifecycle observer with the
same operator/authorization metadata, `--target-role vendor-data-window` (or
`vendor-application` for an eligible complete application), and
`--paired-failure-run <failure-probe output dir>`. The observer requires the
driver's bound source HWND to match the selected lifecycle target; live
data-window tests also require at least 30 seconds of post-loss observation.
If reopening a data window keeps the vendor process alive, use the original
PID and the new top-level data HWND for `gui-restarted`; the observer requires
the original process-creation identity and rejects reuse of the old HWND.
Also pass the rebound window's exact title/image and a vendor-specific
`--new-expected-command-token`; the default `growth_monitor` token is not
valid for a vendor process. A complete vendor-application restart still
requires a distinct PID. Only after both action gates pass may the operator
manually use the approved vendor X/Exit action.

## GUI Lifecycle Test Matrix

| Scenario | Independent fault proof | Required downstream result | Recovery gate |
|---|---|---|---|
| Vendor data-window X | Bound HWND becomes invalid; owning process remains | Target State invalid by deadline; no affected stale values or cached RHEED outputs | Expected data window rebound, then manual reconnect/re-ARM |
| Eligible vendor application Exit | Pre-opened target process handle signals | Same fail-closed behavior; no unrelated PID accepted | Expected executable/command identity and fresh source evidence |
| Growth Monitor X, no floating windows | Main HWND lost and target process exit observed | Session files parseable and quiescent; no residual acquisition | New PID starts idle; manual ARM and START a new session |
| Growth Monitor X, floating windows open | Main HWND lost; same-PID windows and process sampled separately | No worker, classifier, log, or live-looking stale window remains | Close/restart per SOP; new identity and explicit re-ARM |
| Unplanned Growth Monitor disappearance | Pre-existing spontaneous observer sees main HWND/process loss; no retroactive action marker | Same session-integrity and no-residual-acquisition checks | New PID starts idle; preserve incident evidence before re-ARM |
| Abrupt Growth Monitor exit | Offline/tabletop target process exit; independent observer survives | Recoverable evidence is reported without claiming clean shutdown | Restart idle; old session immutable; new linked session |
| Workstation/site-power loss | Second-machine, UPS, or event-log evidence | Same-machine flush evidence is insufficient | Separate post-boot run; never claim same-process continuity |

## Instrument Power Loss

Use `--fault-class instrument-power-loss` only for an exact, approved power
boundary. Never use a generic "instrument power" target. The authorization
must name the device/adapter, explain why it is not control- or safety-critical,
and cite the vendor shutdown/recovery procedure. The script performs no power
operation.

The current executable S3 path is deliberately limited to an approved
Exactus/Modbus pyrometer device or adapter whose COM endpoint is bound to the
active direct-read driver. Camera, MISTRAL, EvapControl, controller, rack, and
shared-supply power tests remain **not tested** until independent power and
device-identity evidence exists.

S3 also requires `--independent-observer-ref`, distinct operator and observer
identities, and an `independent-observer` `failure-start` marker. This records
an observer-asserted power event; it is not a power measurement. A result
describes read-path response and recovery only.

After restoration, verify vendor-ready state, protocol/mode, identity, and
three source-appropriate fresh samples. Mark `gui-reconnected` and `rearmed`;
never automatically resume RUNNING or control.

## Workstation or Process Interruption

The same-process watchdog runs in a dedicated Python thread, independent of
the worker and Qt event loop. It cannot observe a process/GIL/filesystem hang,
workstation outage, or site-power loss. S4 therefore requires second-machine,
UPS, or event-log evidence.

After reboot or abrupt process death, salvage complete fsynced JSONL records:

```powershell
& $python "$checkout\scripts\ombe_failure_probe.py" finalize `
  --output-dir $run
```

`finalize` records the Windows boot ID, reports truncated rows, preserves prior
lifecycle failures, recomputes marker/evidence errors, rejects cross-boot
ordering that could reuse old-boot rows, and rebuilds the SHA-256 manifest.
Post-run markers also refresh the summary automatically. If the watchdog
thread did not terminate, the live process writes no manifest because evidence
may still change. After that recorder process exits, run `finalize --force`
because `summary.json` already exists. For a process/PC interruption with no
summary, use `finalize` without `--force`; the run remains lifecycle-fatal and
cannot be promoted to a pass. Start a separate recovery run after a workstation
outage; do not claim same-process outage coverage.

## Evidence and Acceptance

`states.jsonl` contains State emissions, validity, data age, value changes, and
camera fingerprints. `watchdog.jsonl` contains emission age, worker/driver
identity, passive COM enumeration, local NIC state, and Elog source freshness.

A channel is evaluable only when its own healthy **trailing** baseline spans
the requested duration. The fault window must last at least
`max(2 x healthy p95 cadence, 3 s) + one relevant sample`; a shorter restore is
not evaluable. Link loss, Elog freeze, and recorder emission timeout are
external fault evidence, not proof that the production GUI failed closed.
Only an invalid/error/disconnected target State (or a future production
freshness guard) counts as detection, and it must occur by the stale threshold.
Any State still marked valid after that deadline is a validation failure even
if its displayed value changes. Record:

- invalid/error/disconnected/all-None, emission-timeout, local-link, and
  source-stale detection latencies;
- retained old values and whether logging/classification/images continued;
- first emission, valid value, value/frame/link change after restoration;
- source-appropriate three-sample recovery evidence and its limitation;
- device/interface identity and vendor/GUI/re-ARM actions;
- any automatic resumption of logging or control.

Exit code 0 means a complete passing episode, 2 means recorder/evidence fatal
error, 3 means incomplete or not evaluable, and 4 means a completed validation
failure. Examples include missed detection, retained stale-valid data,
unexpected process residue/exit status, or post-exit writes to the old
session.

## Results

| Tier/source/mode | Target/link | Baseline | Detection channel/latency | Stale retained | Recovery evidence/latency | Re-ARM | Result |
|---|---|---|---|---|---|---|---|
| S1 RHEED WGC | | | | | | | |
| S1 MISTRAL OCR | | | | | | | |
| S1 Evap OCR/Elog | | | | | | | |
| S1 TemperaSure UIA | | | | | | | |
| S2 approved serial/USB | | | | | | | |
| S2 Ethernet - not evaluable | | | | | | | |
| S3 approved device/adapter | | | | | | | |
| S4 observation/tabletop | | | | | | | |
| S1 vendor data-window X | | | | | | | |
| S1 eligible vendor application Exit | | | | | | | |
| S1 Growth Monitor graceful X | | | | | | | |
| S1 Growth Monitor X with floating windows | | | | | | | |
| S0 Growth Monitor abrupt-exit tabletop | | | | | | | |

## Findings

- Confirmed behavior:
- Not evaluable or ineligible scenarios:
- Safety/abort observations:
- Required production hardening:
- Additional approved tests:
