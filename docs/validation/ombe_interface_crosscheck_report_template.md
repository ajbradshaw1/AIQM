# O-MBE Interface Cross-Check Report

> Copy this template for a Bulbasaur test day. Review each run's
> `run_metadata.json`, `summary.json`, `samples.jsonl`, and `SHA256SUMS.txt`
> before entering a conclusion.

## Test Identity

- Date/time, operator, and run IDs:
- Repository commit (must descend from `b24ceb1`):
- Windows version, display resolution, and DPI:
- Hostname and raw artifact root:
- Vendor software and driver versions:
- SHA-256 manifest verification:

The runner is read-only: it never changes setpoints, starts/stops vendor
software, or moves/minimizes windows. Do not unplug equipment or power it down.
Run Exactus and Modbus serial tests separately; close TemperaSure manually
before either one if it owns COM4, then reopen it for the UIA test.

## Durable Test Commands

Run from the repository root. Use the GUI environment's Python. Timed commands
must use the durable launcher and a unique status directory:

```powershell
$repo = (Resolve-Path '.').Path
$python = (Get-Command python).Source
$script = 'scripts\ombe_interface_crosscheck.py'
$launcher = 'C:\Users\Yao_Yufan\.codex\skills\managed-long-jobs\scripts\managed_job.cmd'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$statusRoot = "D:\AI4MBE-job-status\ombe-interface\$stamp"
```

Phase 1 — close TemperaSure manually, then run Exactus:

```powershell
& $launcher launch --output-dir "$statusRoot\exactus" --cwd $repo -- `
  $python $script pyrometer exactus --duration-s 600 `
  --port COM4 --baudrate 115200 --operator YY `
  --software-version 'TemperaSure=5.7.0.4'
```

Poll the job after 30 seconds and at least every 30 seconds thereafter:

```powershell
& $launcher status --output-dir "$statusRoot\exactus"
```

Do not start phase 2 until phase 1 reports `result_available=true`; inspect its
result and exit code. Then run Modbus while TemperaSure remains closed:

```powershell
& $launcher launch --output-dir "$statusRoot\modbus" --cwd $repo -- `
  $python $script pyrometer modbus --duration-s 600 `
  --port COM4 --baudrate 115200 --device-id 1 --operator YY
```

Poll `"$statusRoot\modbus"` to completion. Only then reopen TemperaSure and run
the UIA phase:

```powershell
& $launcher launch --output-dir "$statusRoot\temperasure" --cwd $repo -- `
  $python $script pyrometer temperasure --duration-s 600 --operator YY `
  --software-version 'TemperaSure=5.7.0.4'
```

Poll `"$statusRoot\temperasure"` to completion before starting the remaining
read-only phases:

```powershell
& $launcher launch --output-dir "$statusRoot\evap-paired" --cwd $repo -- `
  $python $script evap-paired --duration-s 600 --operator YY `
  --software-version 'EvapControl=1.2.0.51'
```

Poll `"$statusRoot\evap-paired"` to completion, then:

```powershell
& $launcher launch --output-dir "$statusRoot\mistral-ocr" --cwd $repo -- `
  $python $script mistral-ocr --duration-s 600 --operator YY `
  --software-version 'MistralGui=<record-version>'
```

The runner additionally holds a Windows named mutex for the full Exactus or
Modbus run and fails closed if another diagnostic process owns the same-port
lock. Vendor software does not use that mutex, so the manual TemperaSure
close/reopen gate is still required.

Run short, read-only diagnostics directly:

```powershell
python scripts/ombe_interface_crosscheck.py audit-modbus-config --operator YY
python scripts/ombe_interface_crosscheck.py jsonrpc-diagnose --operator YY
```

## Pyrometer Interfaces

| Mode | Run directory | Attempts / valid | Temperature range | Interval/read p50/p95/p99 | Errors |
|---|---|---:|---:|---:|---|
| Exactus stream | | | | | |
| Modbus RTU | | | | | |
| TemperaSure UIA | | | | | |

Record port-open status, reported device identity, TemperaSure state, and
nonzero exit codes. These runs are not simultaneous, so do not claim
point-by-point agreement.

## Modbus Configuration Audit

- Audit run directory:
- Static finding:
- GUI port/baud expressions passed to `PyrometerWorker`:
- Worker expressions passed to `ModbusPyrometer`:
- Observed Modbus constructor defaults:

This is source-routing evidence only; it does not prove a COM port works.
Hard-coded keyword values do not count as GUI configuration forwarding.

## EvapControl `.elo` vs OCR

- Run directory and EvapControl window title:
- Window sizes and DPI observed:
- `.elo` path(s):
- Valid Elog / OCR / paired samples:
- Unique/reused Elog source timestamps:
- Absolute difference p50/p95/max:
- Absolute Elog-receive to OCR-frame offset p50/p95/max:
- Elog source age at OCR p50/p95/max:
- Crop/text spot-check notes:

Each JSONL row links source/receive timestamps, OCR frame and crop provenance,
raw text, parses, and pair skew. Invalid/out-of-range values are excluded from
differences; repeated Elog timestamps are counted. Never substitute a last
valid value.

## MISTRAL OCR

- Run directory and window title:
- Window sizes and DPI values observed:
- Resize observed within run:
- DPI change observed within or across named runs:
- Parse counts for V/I setpoint and actual:
- All-`None` and OCR-error samples:
- Crop/text spot-check notes:

The capture path reads on-screen pixels. During minutes 0–2 keep the baseline
size, during minutes 2–4 resize smaller, then restore/maximize. Record action
times. Test a second DPI only outside active growth, after the operator changes
Windows scaling and reopens MistralGui; use a new run ID and compare summaries.
Duration alone does not establish resize or DPI coverage.

## MISTRAL JSON-RPC

- Run directory and endpoint:
- HTTP connectivity and read-config key count:
- Four returned values and all-`None` flag:
- Standard discovery methods that succeeded:
- Recorded diagnosis:

Connectivity and readable V/I values are separate observations. A successful
request with empty read configuration is not evidence that data are available.

## Conclusions and Follow-Ups

- Confirmed observations:
- Unresolved observations:
- Candidate fixes for the later hardening branch:
- Additional live tests required:

Keep full crops, JSONL, and durable-job logs on the workstation. Commit only
this report, small summaries, selected example crops, and the raw-data SHA-256
manifest.
