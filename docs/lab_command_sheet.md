# Lab Command Sheet — Bulbasaur / Ch-MBE

Copy-pasteable PowerShell commands for the standard lab-session precheck
sequence. Every command below is **READ-ONLY** unless explicitly noted;
none change device or PLC state.

Referenced scripts live in `scripts/`; each has a `--help` for its own
flag reference. Sequencing follows the [Codex-suggested lab day
order](#recommended-day-order) — deviate as needed for the day's
priorities.

---

## 0. Environment setup (once per session)

```powershell
# From the AIQM repo root
cd C:\path\to\AIQM-Software-Hardware-Integration
git pull
```

Confirm the active chamber:

```powershell
$env:AIQM_CHAMBER          # blank → get_active_config() defaults to O-MBE
$env:AIQM_CHAMBER = "ombe"  # explicit O-MBE
$env:AIQM_CHAMBER = "chmbe" # explicit Ch-MBE — MUST set at shell scope, not inline
```

If working on Ch-MBE, set `$env:AIQM_CHAMBER = "chmbe"` **at the PowerShell
prompt** before invoking any script. Inline `python -c "os.environ['AIQM_CHAMBER']='chmbe'; ..."`
can silently default to O-MBE because `get_active_config()` may already have
run at import time (module-level cache). Setting the env var at the shell
scope, before Python is invoked, avoids that hazard.

Choose an evidence directory once per session so scripts pipe their JSON
into a common location:

```powershell
$evidence = "C:\lab_evidence\{{yyyy-MM-dd}}"          # substitute date
New-Item -ItemType Directory -Path $evidence -Force
```

---

## 1. Fast Mac-side sanity checks (optional, if AJ pulled fresh code)

Runnable from Mac dev env too — no lab hardware needed:

```powershell
python -m pytest -q tests\test_vimba_camera.py                    # VmbCamera + Read fallback
python -m pytest -q tests\test_pyrometer_physical_debug.py        # pyrometer debug logic
python -m pytest -q tests\test_pyrometer_worker.py                # worker + has_valid_reading
python -m pytest -q tests\test_pyrometer_downstream_consumers.py  # log_sensors / heartbeat / commit
python -m pytest -q tests\test_pyrometer_modbus_discover.py       # mocked discovery sweep
python -m pytest -q tests\test_pyrometer_active_exactus_query.py  # mocked Report Version query
python -m pytest -q tests\test_pyrometer_force_modbus.py          # expert-gate logic only
python -m pytest -q tests\test_precheck_mistral_ads.py            # offline ADS precheck logic
python -m pytest -q tests\test_audit_session_sensor_log.py        # sensor-log audit
```

All should exit `0` with green summaries. Failures here mean something
regressed in the pulled code — do NOT proceed until fixed.

---

## 2. O-MBE camera — VmbCamera / kSA coexistence

### 2a. kSA closed — regression guard for the Full path

```powershell
# With kSA 400 fully closed:
python scripts\precheck_direct_camera.py --access-mode auto
```

Expected: `Access mode: full` in the output; verdict READY. If it says
`read`, kSA is still holding the camera despite appearing closed —
check Task Manager for `kSA400.exe` and end it.

### 2b. kSA closed — explicit Full (regression guard)

```powershell
python scripts\precheck_direct_camera.py --access-mode full
```

Same expected behavior; different code path.

### 2c. kSA open — auto (requires Task #187 multicast enabled)

```powershell
# With kSA 400 open on the Live Video window:
python scripts\precheck_direct_camera.py --access-mode auto
```

Two possible outcomes:

- **Task #187 landed**: `Access mode: read`, frames flowing → **PASS**
- **Task #187 NOT landed**: either "Read opened but no frames" or
  "Full denied AND Read denied" — both are useful signal per the
  Read-fallback plan. Read the recovery hint in the verdict block
  and follow it.

### 2d. kSA open — forced Read (isolates Read code path)

```powershell
python scripts\precheck_direct_camera.py --access-mode read
```

Bypasses the auto-fallback. If test 2c is ambiguous, this tells you
whether Read itself is fine and only the fallback path is misbehaving.

---

## 3. O-MBE MISTRAL ADS direct read

### 3a. Direct precheck (with evidence bundle)

```powershell
python scripts\precheck_mistral_ads.py --output-dir $evidence\mistral_ombe
```

Expected: `State: ok` with `6/6 cells populated` (Bulbasaur has 6 cells;
cell7 is not physically present). Any other state — read the recovery
hint.

Common failure hints:
- `connect_failed` → check TwinCAT System Service is running (Windows
  Services), ADS route configured (TwinCAT XAE / SysMan → Routes)
- `provenance_violation` → chamber config mismatch; check
  `drivers/config.py` `ads_cell_count`

### 3b. Short GUI session (for the audit script)

Launch Growth Monitor for O-MBE:

```powershell
python growth_monitor_ombe.py
```

Arm a session, let it collect ≥30s of data, disarm, note the session
directory (printed on session start, format `sessions/YYYY-MM-DD_HH-MM-SS`).

### 3c. Run the sensor-log audit

```powershell
python scripts\audit_session_sensor_log.py path\to\session_dir
```

Expected: `VERDICT: PASS` — the CSV has all 56 ADS union columns,
cells 1-6 populated, cell7 blank, session_metadata.json provenance
consistent.

---

## 4. Pyrometer — RESOLVED Jul 30 2026, read this first

> ### The answer, if you are here because the pyrometer is silent
>
> **On O-MBE, RTS must be de-asserted.** `pyserial` asserts RTS by
> default; on Bulbasaur's IFD-5 (RS232 position) + Prolific PL2303GS
> path an asserted RTS puts the link into **loopback** — every request
> comes back byte-for-byte and the probe never sees it.
>
> Measured on COM4, request `01 03 13 00 00 01 80 8E` (read REG_VER):
>
> | RTS | DTR | Response |
> |-----|-----|----------|
> | True | True | `01 03 13 00 00 01 80 8E` — our own request |
> | True | False | `01 03 13 00 00 01 80 8E` — our own request |
> | False | True | `01 03 02 09 03 FE 15` — **version 9.3** |
> | False | False | `01 03 02 09 03 FE 15` — **version 9.3** |
>
> RTS alone decides it. DTR is irrelevant. This is configured per
> chamber as `MBESystemConfig.pyrometer_rts` (`drivers/config.py`);
> O-MBE is `False` and verified, Ch-MBE is `None` and **uncharacterised**.
>
> **If a pyrometer read returns a verbatim copy of the request, that is
> this bug.** Do not investigate probe modes.

### Validated O-MBE transport settings

```
COM4, 115200 8N1, Modbus ID 1, RTS=False; DTR unimportant on this path.
```

| Setting | Value |
|---------|-------|
| Port | COM4 (Prolific PL2303GS, `VID:PID=067B:23A3`) |
| Baud / framing | 115200 8N1 |
| Modbus device id | 1 |
| **RTS** | **de-asserted (`False`)** |
| DTR | unset — no effect either way |
| IFD-5 switch | RS232 |
| Float layout | high word at the even address (manual p.53) |
| Probe identity | ver 9.3, name/sn `EXI4765` |
| Probe update rate | ~1 Hz (Graph Rate, register `0x1011`) |

Verified end to end: `modbus_discover` → `identity_found_at_default`;
`pyrometer_modbus_smoke.py` → 118 reads, 0 errors; GUI session in
`modbus` mode → 8/8 rows populated, TemperaSure closed.

**Do not poll faster than ~1 Hz and treat the result as independent
samples.** Manual p.53: readings repeat when the host polls faster than
the probe updates. At 2 Hz, 49% of consecutive smoke readings were
identical, and `pyrometer_temp_std_C` reads `0.00` because all five
sub-samples land in one update window. Log it; do not use it for
plateau or change decisions until sampling is aligned. Changing the
probe's update rate is a **config-register write** and is gated like
any other state change.

### FALSIFIED: the "stuck in Exactus mode" hypothesis

The Jul 28 2026 diagnosis — probe in Exactus binary streaming mode with
streaming stopped — is **wrong**. The probe was answering Modbus at
(115200, id=1) throughout; every `silent`, `silent_all_combos`,
`echo_of_sent` and `echo_only` verdict recorded Jul 28 and Jul 30 was
measured through the RTS loopback.

The hypothesis survived a full day of work because it explained every
observation available at the time. Nothing below this line was needed
to fix the actual fault.

**The Exactus-mode recovery ladder in 4.0–4.4 is OBSOLETE for this
setup.** It is retained only as a separate escalation path for a
genuinely nonresponsive system — one that stays silent *after* RTS has
been confirmed de-asserted. Do not enter it before checking RTS.

`pyrometer_force_modbus.py` (4.4) was never run and is not required
here. There was no mode to switch.

---

### TemperaSure port exclusivity (unchanged)

Critical: TemperaSure grabs COM4 **exclusively**. None of the pyrometer
scripts below can share the port with it. The rule is:

> **TemperaSure MUST be CLOSED while these scripts run.** Keep
> TemperaSure available to open BEFORE (verify probe is alive) and
> AFTER (re-verify probe is still alive), but never concurrently. The
> two apps cannot own COM4 at the same time.

### 4.0 Decision flowchart

```
Pyrometer silent when TemperaSure closed?
  │
  ├─ Run pyrometer_physical_debug.py first (READ-ONLY diagnostic)
  │    ├─ Verdict = exactus_streaming / modbus_responsive → use the
  │    │  matching driver in the GUI. STOP.
  │    └─ Verdict = silent / transmitting_unknown / port_down → continue.
  │
  ├─ Run pyrometer_modbus_discover.py (READ-ONLY, broader sweep)
  │    ├─ identity_found_at_default → device is fine at (115200, 1).
  │    │  ModbusPyrometer will read it. STOP.
  │    ├─ identity_found_at_non_default → device is at unexpected
  │    │  (baud, device_id). Reconfigure driver OR use TemperaSure to
  │    │  reconfigure device back to default. STOP (do NOT force).
  │    └─ silent_all_combos → continue to active Exactus query below.
  │
  ├─ Run pyrometer_active_exactus_query.py (non-state-changing WRITE)
  │    Sends the BASF-documented Report Version query (`02 56 56 03`);
  │    interprets the response. Runs BEFORE the state-changing force
  │    script to distinguish "probe alive in Exactus" (no force needed)
  │    from "line has echo" (force is unlikely to help) from true silence.
  │    ├─ exactus_responsive → probe is alive AND currently in Exactus
  │    │  mode. Strong evidence of a working probe. If you need Modbus
  │    │  (which our GUI does), an expert-approved mode switch is still
  │    │  the path back — force_modbus (4.4) OR power-cycle.
  │    ├─ echo_of_sent → serial line is echoing. Investigate cable /
  │    │  DIP-switch / terminal config before running force_modbus.
  │    ├─ query_no_response → true silence. Force is a candidate IF
  │    │  TemperaSure confirms the probe is alive; otherwise physical
  │    │  inspection first.
  │    └─ non_echo_response / echo_plus_extra / partial_echo → inspect
  │       raw hex; don't guess. See Section 4.3 verdict table.
  │
  └─ Run pyrometer_force_modbus.py (state-changing WRITE, expert-gated)
       ├─ Requires --i-am-doing-a-write (safety gate; --dry-run
       │  previews the bytes without touching the port)
       ├─ Re-run pyrometer_modbus_discover.py to confirm the switch
       │  worked. The verdict `wrote_with_response` (plus its
       │  `response_classification` in the evidence bundle) is NEVER
       │  success by itself — see Section 4.4 "Interpreting the verdict."
       └─ If still silent after force-Modbus: fall back to Path A
          (physical power-cycle the probe — BASF's own recommended
          recovery per manual page 53; needs Jiangang's OK). Do NOT
          loop force-Modbus retries.
```

### 4.1 `pyrometer_physical_debug.py` (READ-ONLY, always safe)

```powershell
python scripts\pyrometer_physical_debug.py --output-dir $evidence\pyrometer_debug
```

Or with a longer Exactus listen if the device is expected to be
streaming slowly:

```powershell
python scripts\pyrometer_physical_debug.py --exactus-listen-s 10 `
    --output-dir $evidence\pyrometer_debug
```

Verdict states and next actions:

| State | Next action |
|-------|-------------|
| `exactus_streaming` | Use `ExactusSerialPyrometer` in the GUI. Done. |
| `modbus_responsive` | Use `ModbusPyrometer` in the GUI. Done. |
| `both_active_unexpected` | Either driver works; report to group. |
| `transmitting_unknown` | Continue to 4.2 (discover) — different baud may match. |
| `silent` | Continue to 4.2 (discover) — device may be at non-default (baud, id). |
| `port_down` | Check Device Manager COM assignment + hoggers in Phase 0 output. |

### 4.2 `pyrometer_modbus_discover.py` (READ-ONLY, broader sweep)

Broadens `pyrometer_physical_debug`'s search: 5 baud rates × 11 device
IDs (default) instead of 3 bauds × 1 device_id. Opens the port ONCE per
baud (inner device-ID loop shares the same client), so the full 55-combo
sweep takes ~500 ms of port overhead instead of ~11 s.

**Data-only reads**: touches REG_VER / REG_NAME0 / REG_SN0 / REG_CH1_TEMP
only. Never reads config registers (address, baud, rate, cmd), never
writes anything. Broadcast address 0 is intentionally excluded (Modbus
spec §4.1.1 — write-only convention).

```powershell
python scripts\pyrometer_modbus_discover.py --output-dir $evidence\pyrometer_discover
```

After a probe power-cycle (BASF manual p. 51 documents 5-10 s self-init),
give the device time to boot with a **one-shot** wait — this is NOT
per-combo, just the initial pre-sweep pause:

```powershell
python scripts\pyrometer_modbus_discover.py --initial-wait-s 8 `
    --output-dir $evidence\pyrometer_discover
```

For debugging multiple devices on the bus, walk the full matrix
instead of early-stopping on the first hit per baud:

```powershell
python scripts\pyrometer_modbus_discover.py --exhaustive `
    --output-dir $evidence\pyrometer_discover
```

Verdict states and next actions:

| State | Next action |
|-------|-------------|
| `identity_found_at_default` | Device is at (115200, 1) — the driver default. `ModbusPyrometer` will read it. Done. |
| `identity_found_at_non_default` | Device answered at a non-default (baud, id). **This script does NOT reconfigure** — either update `ModbusPyrometer` construction to match OR use TemperaSure to reconfigure the device back. |
| `silent_all_combos` | Device isn't answering Modbus at any combo. If TemperaSure confirms the probe is alive, force-Modbus experiment (4.3) is a candidate. Otherwise physical inspection. |
| `pymodbus_missing` | Install `pymodbus`: `pip install pymodbus pyserial`. Env issue, not a device issue. |

### 4.3 `pyrometer_active_exactus_query.py` (non-state-changing WRITE)

Sits between the READ-ONLY sweep (4.2) and the state-changing force
script (4.4). Sends the BASF-documented Report Version query
(`02 56 56 03`, manual page 51) and interprets the response. **Does
NOT change probe state** — Report Version is a query, not a control
command. Because it puts bytes on the wire, though, it is not
read-only; the docstring's phrasing is "non-state-changing, not
read-only."

Trust contract enforcement: an `ALLOWED_QUERIES` whitelist inside the
script rejects any payload other than `REPORT_VERSION_BYTES` before
opening the port. Adding a new query to the whitelist requires a
manual-page citation, a docstring update, and updating the
`test_only_report_version_is_allowed` assertion — same discipline as
`pyrometer_physical_debug.py`'s READ-ONLY invariant.

**Preview the bytes** (safe, opens no port):

```powershell
python scripts\pyrometer_active_exactus_query.py --dry-run
```

**Actual query** (no safety gate — non-state-changing):

```powershell
python scripts\pyrometer_active_exactus_query.py `
    --output-dir $evidence\pyrometer_active_query
```

Extend the response window if the probe is slow to answer:

```powershell
python scripts\pyrometer_active_exactus_query.py --response-window-s 3.0 `
    --output-dir $evidence\pyrometer_active_query
```

Verdict states and next actions:

| State | Meaning | Next action |
|-------|---------|-------------|
| `exactus_responsive` | Response starts with `02 95` (BASF Report Version reply). Probe is alive AND currently in Exactus mode. This is strong evidence of a working probe — it does NOT mean the mode-switch is unnecessary in general. | To prove the probe is alive: done, no further action needed. To get Modbus (which our GUI needs): an expert-approved mode switch is still the path back — run `pyrometer_force_modbus.py` (4.4) OR power-cycle (BASF's recommended recovery per manual page 53). To keep using Exactus (rare — we're moving away from that): no action. |
| `echo_of_sent` | Response exactly matches the sent bytes (`02 56 56 03`). Consistent with USB-serial local echo (not proven — see `[[prolific-pl2303-local-echo]]`). | Investigate cable / DIP-switch / terminal config BEFORE running force. Force is unlikely to help if the line is echoing. |
| `echo_plus_extra` | Response starts with sent bytes, followed by more. | Inspect raw hex; may be echo + ACK or echo + noise. |
| `partial_echo` | Response is a proper prefix of the sent bytes. | Inspect raw hex; driver flush artifact or truncated echo. |
| `non_echo_response` | Response bytes differ from what we sent AND don't start with `02 95`. | Inspect raw hex; could be NAK (`0x15` per manual page 51), noise, or unexpected framing. |
| `query_no_response` | 0 bytes received. True active silence (we sent a query and heard nothing back). | If TemperaSure confirms probe is alive → `force_modbus` (4.4) is a candidate. Otherwise physical inspection. |
| `port_error` | Serial open or write raised. | Check TemperaSure isn't running, cable is seated, port matches Device Manager. |
| `dry_run` | Preview only. Nothing sent. | N/A |

`version_info` sub-field (evidence bundle only, present iff verdict is
`exactus_responsive`):

- `shape` — one of `complete` (13 bytes, matches BASF spec), `truncated`
  (starts with `02 95` but shorter), or `extra_bytes` (starts with
  `02 95` but longer). Documents the length mismatch without failing.
- `version_byte` — the single byte after `02 95` (e.g. `0x44` in BASF's
  example)
- `prom_hex` — the 9-byte PROM code (factory-use identifier)

### 4.4 `pyrometer_force_modbus.py` (state-changing WRITE, expert-gated)

**Read Section 4.0 flowchart before considering this script.** This is
the ONLY state-changing pyrometer script in the repo. It sends the BASF-
documented Exactus→Modbus mode-switch command `02 4D 4D 03`
(BASF Exactus User Manual v5.04 page 51 — "Switch to Modbus Mode",
Format: STX 0x4D 0x4D ETX, Response: None).

**BASF's own recommended recovery** from TemperaSure/special-mode is
Path A (power-cycle the probe — manual page 53: "To return the probe
to Modbus mode, disconnect the probe from the computer and cycle the
power to the probe"). This script (Path B) is a documented alternative
for when power-cycle is not immediately available.

**Pre-flight**:

1. Run `pyrometer_modbus_discover.py` first (Section 4.2). Only proceed
   if it returned `silent_all_combos`.
2. TemperaSure MUST be **closed**. Open TemperaSure briefly BEFORE
   running this script to confirm the probe is alive → close TemperaSure
   → run force_modbus → close force_modbus → optionally reopen
   TemperaSure to confirm the probe is still alive.
3. Have Path A authorization from Jiangang before starting — if this
   script doesn't produce `identity_found_at_default` on the follow-up
   discover pass, the fallback is power-cycle (BASF's primary recovery).

**Preview the bytes without touching the port** (always safe, works
without pyserial):

```powershell
python scripts\pyrometer_force_modbus.py --dry-run
```

**Actual write** (requires the explicit ack flag):

```powershell
python scripts\pyrometer_force_modbus.py --i-am-doing-a-write `
    --output-dir $evidence\pyrometer_force_modbus
```

Add `--verify` to print the next-step command (the discover invocation)
after the run — the script does NOT auto-invoke it:

```powershell
python scripts\pyrometer_force_modbus.py --i-am-doing-a-write --verify `
    --output-dir $evidence\pyrometer_force_modbus
```

**Interpreting the verdict** — repeat this to yourself before acting:

| State | Meaning | Success? |
|-------|---------|----------|
| `dry_run` | Preview only, nothing sent | N/A |
| `wrote_no_response` | Write completed, no bytes echoed. Matches BASF's documented `Response: None` (manual page 51). | **Unknown**. Verify via 4.2. |
| `wrote_with_response` | Write completed, some bytes came back. Look at `response_classification` in the evidence bundle to know what shape. | **NOT SUCCESS by itself** — see classification below. Verify via 4.2. |
| `port_error` | Serial open or write raised. | Failure. Check TemperaSure isn't running, cable is seated, port matches Device Manager. |
| `safety_denied` | Neither `--i-am-doing-a-write` nor `--dry-run` was set. Nothing sent. | N/A |

**Response classification** — when the verdict is `wrote_with_response`, the
evidence bundle also records `response_classification` describing what shape
the received bytes had. Labels are observational (describe what was seen);
they do NOT assert causation:

| Classification | What was seen | What it's consistent with (NOT proven) |
|----------------|---------------|-----------------------------------------|
| `echo_of_sent` | Response bytes exactly match sent bytes (`02 4D 4D 03`) | USB-serial local echo (matches Jacques's notebook pattern). Also possible: physical TX/RX short at connector, terminal-side echo config. Not proof of probe response. |
| `echo_plus_extra` | Response starts with sent bytes, followed by more | Could be echo + probe ACK, echo + noise, echo + stale buffer. Inspect raw hex; do not guess. |
| `partial_echo` | Response is a proper prefix of sent bytes | Truncated echo, driver flush artifact. Inspect raw hex. |
| `non_echo_response` | Response bytes differ from what we sent | Novel bytes on the wire. Could be probe data, noise, partial framing. Inspect raw hex. |
| `no_response` | 0 bytes received (only appears if the state were somehow set — normal `wrote_no_response` verdict already covers silence) | Silence. |

**Neither the state nor the classification is a success flag.** Success is
proven ONLY by re-running `pyrometer_modbus_discover.py` and getting
`identity_found_at_default`. `echo_of_sent` is diagnostic-worthy (it
suggests the serial line has an echo issue that made prior silent verdicts
unreliable) but it is not proof of anything about the probe's mode.

If verification returns `silent_all_combos` again, **do NOT retry the
force write in a loop**. Fall back to Path A (physical power-cycle,
BASF's own recommended recovery per manual page 53). Record the outcome
in the day's memory update so the next session inherits the finding.

---

## 5. Ch-MBE regression (if working on Ch-MBE that session)

### 5.0 Session discipline — read before the first command

Two rules, both written after the Jul 30 2026 O-MBE session where their
absence cost hours.

**Evidence discipline.** Every test gets its **own** `--output-dir`.
Two scripts sharing one directory both write `result.json` and the
second silently clobbers the first — this happened on Jul 30 and the
first bundle was only recoverable because the console scrollback
survived. For each test record:

- the exact command as typed
- **which apps were open**, especially TemperaSure — its open/closed
  state during two Jul 30 tests is permanently unrecoverable, and it
  determines whether those results mean anything
- port, baud, device id
- **RTS and DTR state** whenever serial is involved
- **raw RX bytes** whenever serial is involved — not the library's
  interpretation of them

**Stop conditions.** Do **not** change Ch-MBE config, write to probe
registers, or attempt any mode switch until a **read-only identity
exchange has been captured and reviewed**. On Jul 30 a full state-change
ladder was built and nearly executed against a probe that was answering
correctly the whole time; the thing that would have prevented it was
one read-only exchange with the raw bytes visible.

### 5.1 Ch-MBE hypotheses — NOT inherited O-MBE facts

Everything O-MBE established on Jul 30 is chamber-specific until
measured here. Treat each of these as an open question:

| Item | O-MBE (verified) | Ch-MBE status |
|------|------------------|---------------|
| `pyrometer_rts` | `False` | **`None` — uncharacterised.** Test both ways |
| Pyrometer port | COM4 | **Unverified.** Config default was never checked |
| Float word order | high word first | **Unconfirmed.** Report both, pick the physical one |
| Elog log dir | nested `_Omicron_Software` path | **Flat** `C:\evap_control_1.2.0.48\log` — never exercised |
| Elog var map | all 11 names matched | **Known to differ** (different cell names) |
| Pressure units | ADS agrees with elog to ~1% | **Different gauge.** ADS is the *primary* path here |
| TemperaSure title | `...5.7.0.4 Advanced Mode` | `...5.7.0.4` — **no suffix** |
| Cell count | 6, cell7 blank | **7, all populated** |

If a Ch-MBE serial read returns a **verbatim copy of the request**,
that is the RTS loopback described in Section 4 — set
`pyrometer_rts=False` in `CHALCOGENIDE_MBE` and re-verify. Do not set
it before observing that.

### 5.2 MISTRAL ADS regression

Set the chamber and re-do the MISTRAL ADS precheck:

```powershell
$env:AIQM_CHAMBER = "chmbe"
python scripts\precheck_mistral_ads.py --output-dir $evidence\mistral_chmbe
```

Expected: `State: ok` with `7/7 cells populated` (Ch-MBE has 7 cells)
and `ads_display_confirmed: True` in the Phase 1 config summary.

Then a short GUI session + audit:

```powershell
python growth_monitor_chmbe.py
# Arm, collect ≥30s, disarm, note the session dir
python scripts\audit_session_sensor_log.py path\to\chmbe_session_dir
```

Audit `VERDICT: PASS` should show 7/7 cells populated (no blank cell7
this time).

---

## 6. Opportunistic Ch-MBE tasks (if time remains)

None of these have precheck scripts yet — they're pending investigations.
Include here so nothing gets forgotten:

- **Task #189** — LakeShore 335 SCPI direct-read on COM4
- **Task #190** — kSA SQL query interface for RHEED metrics
- **Task #195** — Diagnose why MISTRAL screengrab failed on Ch-MBE

If any of these get exploratory work at the lab, capture findings in a
dated `docs/lab_validation_YYYY_MM_DD_*.md` file.

---

## Recommended day order

Per Codex Jul 28 sequencing:

1. Section 0 + 1 (pull, sanity, environment)
2. Section 2 (O-MBE camera coverage)
3. Section 3 (O-MBE MISTRAL ADS end-to-end)
4. Section 4 (pyrometer — BEFORE anything TemperaSure-related)
5. Section 5 (Ch-MBE regression, if applicable)
6. Section 6 (opportunistic — only if time)

---

## Post-lab

- Push any evidence bundles to Slack or archive under
  `docs/lab_evidence/YYYY-MM-DD/` (SOP TBD — for now, keep JSONs local
  and paste relevant excerpts).
- Update the Claude memory `bulbasaur_lab_day_YYYY_MM_DD.md` with the
  day's outcomes (Claude will do this on request — say something like
  "update the memory files with today's work").
- If any code changes were pushed from the lab, note them in the memory
  cross-referenced to their commit hashes.
