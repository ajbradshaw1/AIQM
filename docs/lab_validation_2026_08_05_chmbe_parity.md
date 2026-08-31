# Ch-MBE Parity Validation — 2026-08-05

Bench checklist for closing the O-MBE → Ch-MBE parity gaps identified in the
2026-08-05 chamber-config audit. Run on the Ch-MBE / Omicron PC.

Every command below is **READ-ONLY** unless explicitly marked otherwise. No
step changes probe configuration, PLC state, or growth settings. The one
state-changing script in the repo (`pyrometer_force_modbus.py`) is
explicitly out of scope — see [Do not run](#do-not-run).

Companion to `lab_command_sheet.md` §5. That file is the evergreen SOP; this
one is a dated, fill-in-as-you-go record.

---

## Why these specific tests

Both chambers run the **same** `GrowthApp`. There is no separate Ch-MBE GUI.
The only divergence is which `MBESystemConfig` `get_active_config()` returns,
so parity is a question of configuration and measurement, not code.

The 2026-08-05 audit found `MBESystemConfig` has 22 fields, of which **11 are
inert** — declared with per-chamber values that nothing reads:

```
evap_log_dir          pyrometer_port          single_images_folder
ksa_window_title      pyrometer_baudrate      stream_images_folder
temperasure_title     pyrometer_device_id     camera_index
temperasure_exe       camera_fps
```

The live ones are `name`, `chamber_id`, `mistral_mode_default`,
`evap_mode_default`, `cell_display`, `pyrometer_rts`, and the five `ads_*`
fields. Consequence: several values that *look* configured for Ch-MBE do
nothing, and the drivers fall back to hardcoded defaults or their own
`KNOWN_*` candidate lists. These tests establish what the hardware actually
does, so the config can be corrected rather than assumed.

---

## Open questions this run should close

| # | Question | O-MBE (validated) | Ch-MBE status | Phase | Result |
|---|---|---|---|---|---|
| 1 | Elog variable names | 11 names matched | **Known to differ** — blocks elog mode | 2 | ✅ **CLOSED** — 78-var schema dumped, live values verified |
| 2 | Pyrometer COM port | COM4 | **Unverified** | 0 / 3 | ✅ **COM3** |
| 3 | Pyrometer baud + slave ID | 115200, id 1 | **Unverified** | 3 | ✅ baud **115200**; slave ID still open |
| 4 | `pyrometer_rts` | `False` (verified Jul 30) | **`None` — uncharacterised** | 3 | ⬜ open |
| 5 | Float word order | high word at even addr | **Unconfirmed** | 3 | ⬜ open |
| 6 | kSA window title + chrome | measured on Bulbasaur | **Unverified** | 0 / 4 | ➖ descoped — kSA off, Vimba direct |
| 7 | Camera model + bit depth | Manta G-033B, 12-bit | **Unverified** (`_max_value=4095` hardcoded) | 4 | ✅ **Manta G-033B, 12-bit — hardcoded value is CORRECT** |
| 8 | Vimba Read-mode coexistence | works (Task #187) | **Unverified** | 4 | ➖ descoped — Full mode only |
| 9 | kSA palette vs. reference | byte-verified, 200 BMPs | **Unverified** | 4 | ➖ superseded by #11 |
| 10 | ADS 7-cell path | 6 cells, validated | validated Jul 22 — regression check | 1 | ⬜ open |
| 11 | **Camera exposure / gain** | camera user set, unrecorded | **Unverified — now the main classifier-input risk** | 4 | ⬜ open |

### 2026-08-05 decision — kSA off, Vimba direct

Dual capture is abandoned on Ch-MBE. kSA is switched off and the Vimba
camera is read directly. This closes questions #6, #8, #9 by removing the
screengrab path entirely, and it means `access_mode` is always `full`.

It also **promotes question #11**. With both chambers on the same camera
model and the same fixed `KSA_BGW_PALETTE`, the raw→palette transform is
identical — so Ch-MBE Vimba output should match Bulbasaur Vimba output,
which was byte-verified against the training BMPs. The one input that can
still break that chain is **exposure and gain**, which `VmbCamera` does not
set: they come from each camera's persistent user set. Different exposure
means a different intensity distribution into the classifier regardless of
model and palette.

Side effects to confirm with the team: turning kSA off also drops kSAComm
(Task #190, kSA SQL RHEED metrics) and changes the grower-facing workflow,
since kSA is the tool growers normally watch.

---

## Session discipline

Carried forward from the Jul 30 2026 O-MBE session, where their absence cost
hours.

**Evidence discipline.** Every test gets its **own** `--output-dir`. Two
scripts sharing one directory both write `result.json` and the second
silently clobbers the first. For each test record:

- the exact command as typed
- **which apps were open** — especially TemperaSure, kSA, and MISTRAL
- port, baud, device id
- **RTS and DTR state** whenever serial is involved
- **raw RX bytes** whenever serial is involved, not the library's reading of them

**Stop conditions.** Do not change Ch-MBE config, write to probe registers,
or attempt any mode switch until a **read-only identity exchange has been
captured and reviewed**. On Jul 30 a full state-change ladder was nearly
executed against a probe that was answering correctly the entire time.

**Machine availability.** Phases 0–2 do not touch the camera, the serial
port, or kSA, and can run alongside other work. Phase 3 requires exclusive
COM-port access (TemperaSure closed). Phase 4 changes kSA and camera state
and is the most disruptive to a grower.

---

## Phase 0 — provenance and inventory

No hardware contact. Establishes what is actually being tested — important
when the machine has been used for other branch testing the same day.

```powershell
git rev-parse --short HEAD; git status --short
```

RESULT — commit: `__________`  working tree clean? `____`

```powershell
python -m pip check
```

RESULT: `__________________________________________`

```powershell
$env:AIQM_CHAMBER = "chmbe"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$evidence = "D:\AIQM-evidence\chmbe_$stamp"
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
```

Confirm the chamber actually took — `growth_monitor_chmbe.py` uses
`os.environ.setdefault`, so a stale `ombe` value in this shell would **not**
be overridden:

```powershell
$env:AIQM_CHAMBER
```

RESULT: `__________` (must read `chmbe`)

### 0.1 Serial port enumeration

Closes question #2. Do **not** assume COM4 — that is the hardcoded GUI
fallback and the `pyrometer_port` config value is inert.

```powershell
python -m serial.tools.list_ports -v
```

RESULT — pyrometer port: **COM3** (confirmed 2026-08-05) — note this is
**not** COM4, the hardcoded GUI fallback and the inert `pyrometer_port`
config default. Baud confirmed **115200**, same as O-MBE.

VID:PID: `__________`  adapter: `__________________`

### 0.2 Window titles

Closes question #6, and is the first diagnostic for **Task #195** (MISTRAL
screengrab failed on Ch-MBE). Open kSA, TemperaSure, MISTRAL and EvapControl
first.

```powershell
python scripts\probe_windows.py
```

RESULT — kSA Live Video title: `__________________________________`

RESULT — TemperaSure title: `__________________________________`

RESULT — MISTRAL title: `__________________________________`

RESULT — values UIA-readable, or OCR required? `__________`

While kSA is open, also record the chrome geometry the capture path assumes
(title bar + menu + toolbar = 75 px top, status bar = 30 px bottom, both
measured on Bulbasaur 2026-04-25):

RESULT — title bar `____` menu `____` toolbar `____` status bar `____`

### 0.3 Python environment

**Ch-MBE runs a venv** at `C:\Users\Omicron\AIQM-Software-Hardware-Integration\.venv`
— unlike Bulbasaur, which uses system Python with no venv. Activate before
any command in this document. Repo path confirmed 2026-08-05.

`requirements.txt` declares 11 packages but the real runtime set is ~22; the
difference is all lazy imports (`vmbpy`, `mss`, `pywinauto`, `torch`,
`scipy`, `openpyxl`, `pyqtgraph`, `pandas`, `reportlab`, `crccheck`,
`simple_pid`), none of which it lists. **Checking against `requirements.txt`
alone gives a false all-clear.** Use an import-level check:

```powershell
python -c "import importlib.util as u; mods=[('PyQt6','PyQt6'),('numpy','numpy'),('matplotlib','matplotlib'),('sklearn','scikit-learn'),('PIL','Pillow'),('cv2','opencv-python'),('pyvisa','pyvisa'),('pytesseract','pytesseract'),('pymodbus','pymodbus'),('serial','pyserial'),('pyads','pyads'),('vmbpy','vmbpy'),('mss','mss'),('pywinauto','pywinauto'),('torch','torch'),('scipy','scipy'),('openpyxl','openpyxl'),('pyqtgraph','pyqtgraph'),('pandas','pandas'),('reportlab','reportlab'),('crccheck','crccheck'),('simple_pid','simple-pid')]; [print(('OK   ' if u.find_spec(m) else 'MISS ')+f'{m:14} pip:{p}') for m,p in mods]"
```

RESULT (2026-08-05): `pip check` clean. All runtime-critical packages
present — `vmbpy 1.2.2`, `pymodbus 3.14.0`, `pyserial 3.5`, `pyads 3.6.0`,
`torch 2.13.0`, `opencv-python 5.0.0.93`, `PyQt6 6.11.0`.

Missing, none blocking: `reportlab` (PDF generator only), `simple_pid`
(heater dashboard, not reachable from Growth Monitor), `crccheck` (Dracal
thermocouple only, lazy-imported).

Also present: `windows-capture-interpreter 1.5.1` — Yao's WGC dependency,
unused now that the chamber is Vimba-direct. The venv additionally carries
Flask, gunicorn, `google-api-*`, `psycopg2-binary`, `timm`, `torchvision`,
which are not AIQM dependencies; harmless, but this is not a clean-room env.

---

## Phase 1 — MISTRAL ADS

Read-only. `precheck_mistral_ads.py` is the **only** chamber-aware script in
`scripts/`, and ADS is the best-configured parity path — `ads_netid`,
`ads_cell_count`, `ads_port_main`, `ads_port_pid` and
`ads_display_confirmed` are all genuinely consumed.

```powershell
python scripts\precheck_mistral_ads.py --output-dir "$evidence\mistral_ads"
```

Expected: `State: ok`, `7/7 cells populated`, `ads_display_confirmed: True`,
NetID `10.0.42.112.1.1`.

RESULT — state: `__________`  cells: `____/7`  netid: `________________`

```powershell
python scripts\test_ads_read.py --output "$evidence\ads_snapshot.csv"
```

RESULT: `__________________________________________`

Common failures: `connect_failed` → check the TwinCAT System Service is
running and the ADS route is configured. `provenance_violation` → chamber
config mismatch, check `ads_cell_count`.

---

## Phase 2 — EvapControl `.elo`

Closes question #1 — the largest *functional* parity gap.
`CHALCOGENIDE_MBE.evap_mode_default` is `"screengrab"`, not `"elog"`, purely
because the Ch-MBE variable map was never confirmed. The validated direct-read
path is therefore inactive on this chamber.

### 2.1 Confirm today's log exists

```powershell
Get-ChildItem "C:\evap_control_1.2.0.48\log" -Filter "log_$(Get-Date -Format yyyy-MM-dd)_*.elo"
```

RESULT (2026-08-05): `log_2026-08-05_000000.elo` — **Length 0**, written
12:00 AM. ⛔ **Empty file.**

Note EvapControl rotates at local midnight, but a mid-day start yields a
`_HHMMSS` suffix rather than `_000000`. Both are live files.

> **A 0-byte `.elo` breaks the schema dump in 2.2.** `parse_schema()` reads
> an 8-byte header and `struct.unpack(">II", b"")` raises
> `struct.error: unpack requires a buffer of 8 bytes`. `ElogReader` uses the
> same parser, so 2.3 fails identically. Verified 2026-08-05.
>
> A midnight-rotated file at 0 bytes means EvapControl has written nothing
> today — most likely it is not running, or is running without logging.

### 2.1b Recovery — the schema is in *any* `.elo` from this machine

The variable map lives in the schema header, not in the data. **Any**
non-empty `.elo` ever written on this machine carries it, so an older file
answers question #1 just as well as today's.

```powershell
Get-ChildItem "C:\evap_control_1.2.0.48\log" -Filter "*.elo" | Sort-Object Length -Descending | Select-Object -First 10 Name, Length, LastWriteTime
```

RESULT — largest non-empty `.elo`: `__________________  ______ bytes`

If one exists, run 2.2 against it by absolute path. If **every** `.elo` is
0 bytes, EvapControl has never logged on Ch-MBE — start it, let it run a few
minutes, and re-check before concluding the format differs.

### 2.2 Enumerate the variable schema — the actual deliverable

The `.elo` format carries its own variable-name schema in the header, so the
Ch-MBE map can be read directly rather than inferred from what fails.

```powershell
python -c "from drivers.elog import find_current_log, parse_elog; p=find_current_log(r'C:\evap_control_1.2.0.48\log'); print(p); n,t,v=parse_elog(p); print(len(n),'vars'); [print(f'{i:3} {nm:45} {v[-1][i]}') for i,nm in enumerate(n)]" > "$evidence\chmbe_elog_schema.txt"
```

RESULT — variable count: `____`   file saved? `____`

**This file is the highest-value artifact of the session.** It is what makes
`evap_mode_default="elog"` switchable on for Ch-MBE.

### 2.3 Confirm the GUI's reader opens it

```powershell
$env:AIQM_EVAP_LOG_DIR = "C:\evap_control_1.2.0.48\log"
python -c "from drivers.evap_control import ElogReader; r=ElogReader(); r.connect(); print(r.read()); r.disconnect()"
```

Blank O-MBE cell fields here are **expected** and confirm the remaining work
is name mapping, not file access.

RESULT (2026-08-05): file access **works**. `chamber_pressure_mbar` =
`1.295e-09` populated; all ten other `DEFAULT_VAR_MAP` fields `None`.
Confirms the remaining work is name mapping, not file access.

> **Do not run `scripts\test_elog.py` unchanged.** It carries a hardcoded
> `PANEL` of O-MBE variable names and will only report those names missing.

### 2.4 Results — Ch-MBE schema (2026-08-05) ✅

**78 variables.** `MBE.Pressure` is the *only* name shared with
`ElogReader.DEFAULT_VAR_MAP`. Live values confirmed via `latest_record()` at
19:43 UTC:

| Variable | Value | Note |
|---|---|---|
| `MBE.Pressure` | 1.296e-09 mbar | matches the `ElogReader.read()` value |
| `Manipulator.PV` | 44.2 °C | **substrate temperature** — the `MBE-Mani.PV` equivalent |
| `HTEZ_Fe.PV` | 600.0 °C | Fe, standby |
| `NTEZ1_Te.PV` | 275.0 °C | Te, standby |
| `NTEZ2_Se.PV` | 60.0 °C | Se, standby |

**Source inventory** — each effusion source exposes 8 fields
(`.OP` W, `.PV` °C, `.flow` l/min, `.mode`, `.rate`, `.setpoint` °C,
`.shutter`, `.wSP`), far richer than O-MBE's 5 PV-only entries:

| Prefix | Material |
|---|---|
| `HTEZ_Fe` | Fe |
| `NTEZ1_Te` | Te |
| `NTEZ2_Se` | Se |
| `NTEZ3_`, `NTEZ4_`, `WEZ_` | unloaded |
| `EBVM` | e-beam evaporator (voltage, current, pocket, status) |
| `Manipulator` | substrate |

Pressure gauges available: `MBE.Pressure`, `MBE.IGPpressure`, `MBE.Pirani`,
`BFM.Pressure`, `Intro.Pressure`, `Intro.Pirani`, plus turbo speed/current
for both `MBE.*` and `Intro.*`.

**No `Plasma.*` variables** — Ch-MBE has no plasma source, so those three
`DEFAULT_VAR_MAP` entries are permanently N/A here.

#### ⚠️ Trap — the manipulator setpoint is in AMPS, not °C

```
Manipulator.PV         %.1f °C     ← temperature
Manipulator.setpoint   %.2f A      ← CURRENT
Manipulator.wSP        %.2f A      ← CURRENT
Manipulator.OP         %.2f A
```

Ch-MBE's manipulator is **current-controlled** where O-MBE's is
temperature-controlled. Mapping `Manipulator.setpoint` →
`substrate_temp_setpoint_C` would log amps into a field labelled °C. There
is no °C setpoint on this chamber — this needs a **new field**, not a remap.

#### Candidate ADS cell mapping (hypothesis, needs Jiangang)

`CHALCOGENIDE_MBE` records *"Cell2–7 physical mapping pending Jiangang
confirmation."* Manipulator + the six effusion sources = **7**, matching
`ads_cell_count=7`. Strong candidate, but `EBVM` is an eighth source, so the
ADS `Cell{N}` **ordering** still needs confirming against these names before
`cell_display` labels are changed.

#### 🐛 Bug found — format strings decode as mojibake

`parse_schema()` decodes with `.decode("utf-8", errors="replace")`, but the
`.elo` is **cp1252**, where `°` is `0xB0` — invalid standalone UTF-8. Result:

```
HTEZ_Fe.PV      %.1f �C
```

Variable *names* are ASCII and unaffected, but the **format strings are
corrupted**, and `format_value()` renders them straight through:

```
format_value(600.0, '%.1f �C')  ->  '600.0 �C'      # verified 2026-08-05
b'\xb0'.decode('cp1252')        ->  '°'             # the fix
```

**This is pre-existing and affects both chambers** — O-MBE's `.elo` almost
certainly carries the same degree signs. It simply surfaced here first. Fix:
try cp1252, fall back to utf-8. Worth doing before elog mode goes live on
either chamber.

#### Scope note for the mapping work

Ch-MBE cell **temperatures already arrive via ADS** (`cell_display` has
`state_field=None`, 7/7 populated). Do not duplicate them through elog. The
elog's unique contribution on this chamber is what ADS does not provide:
shutter states, control modes, ramp rates, cooling flow, setpoints, e-beam
status, and the additional pressure gauges.

Per `DEFAULT_VAR_MAP`'s own comment, adding fields requires extending
`EvapControlState` in `gui/state.py` **and** the `log_sensors` schema in
`gui/growth_logger.py` so new columns reach `sensor_log.csv`. A `var_map`
dict alone is not sufficient.

---

## Phase 3 — pyrometer

Closes questions #3, #4, #5. **TemperaSure must be CLOSED** — it owns the
serial port exclusively. Open it first to confirm the probe is alive and to
record its window title, then close it before any command below.

Substitute the port found in Phase 0.1 for `COM4` throughout.

### 3.1 Passive listen — no traffic on the wire

```powershell
python scripts\pyrometer_physical_debug.py --port COM4 --skip-modbus --exactus-listen-s 10 --output-dir "$evidence\pyro_passive"
```

RESULT — verdict: `__________`  bytes seen: `__________`

### 3.2 Read-only Modbus sweep

5 bauds (115200, 57600, 38400, 19200, 9600) × 11 slave IDs (1–10, 247).
Reads only firmware version, name, serial number, and channel-1 temperature.
Never touches config registers.

```powershell
python scripts\pyrometer_modbus_discover.py --port COM4 --no-rts --output-dir "$evidence\pyro_discover_norts"
```

RESULT — verdict: `__________`  baud: `______`  id: `____`

**If `silent_all_combos`, run once more with RTS asserted before concluding
anything:**

```powershell
python scripts\pyrometer_modbus_discover.py --port COM4 --rts --output-dir "$evidence\pyro_discover_rts"
```

RESULT — verdict: `__________`  baud: `______`  id: `____`

> Why both: the scripts default to `--no-rts`, the O-MBE-validated state. But
> `CHALCOGENIDE_MBE.pyrometer_rts` is `None`, meaning the GUI leaves
> pyserial's default — **asserted**. So `--rts` reproduces what the Ch-MBE GUI
> does *today*; `--no-rts` tests the O-MBE finding. Record which is which.

### 3.3 Raw-byte capture at the answering combination

Satisfies the stop condition: a read-only identity exchange with raw bytes
visible, reviewed before any state change is considered.

```powershell
python scripts\pyrometer_raw_modbus_probe.py --port COM4 --baud <found> --device-id <found> --no-rts --output-dir "$evidence\pyro_raw_norts"
```

```powershell
python scripts\pyrometer_raw_modbus_probe.py --port COM4 --baud <found> --device-id <found> --rts --output-dir "$evidence\pyro_raw_rts"
```

RESULT — `--no-rts` raw RX: `__________________________________`

RESULT — `--rts` raw RX: `__________________________________`

A response that is a **verbatim copy of the request** is the RTS loopback
signature O-MBE had — not a dead probe. If seen, the *other* RTS state is the
live one, and `pyrometer_rts` should be set accordingly in
`CHALCOGENIDE_MBE` **after** verifying against the probe.

### 3.4 Float word order

```powershell
python scripts\pyrometer_modbus_smoke.py --port COM4 --id <found> --hz 1 --duration 60 --output "$evidence\pyro_smoke_plain.json"
```

```powershell
python scripts\pyrometer_modbus_smoke.py --port COM4 --id <found> --hz 1 --duration 60 --word-swap --output "$evidence\pyro_smoke_swapped.json"
```

Keep whichever yields a physically sensible temperature. Note this script
uses `--id` and `--output`, unlike the `--device-id` / `--output-dir` used
elsewhere.

RESULT — plain: `______ °C`   swapped: `______ °C`   correct: `__________`

Do not poll faster than ~1 Hz — the probe's own update rate. Faster sampling
repeats values and drives `pyrometer_temp_std_C` to 0.00.

---

## Phase 4 — camera and kSA

Closes questions #7, #8, #9. Most disruptive phase — coordinate with whoever
is using the chamber.

### 4.1 Coexistence first, kSA OPEN

A Full-mode pass proves nothing about coexistence, so test Read first.

```powershell
python scripts\precheck_direct_camera.py --access-mode read --allow-dark-frame
```

RESULT — negotiated mode: `__________`  frames flowing? `____`

```powershell
python scripts\probe_vimba_access_modes.py --output-dir "$evidence\vimba_modes"
```

RESULT: `__________________________________________`

A failure here likely means Ch-MBE has not enabled Vimba multicast / read
sharing (the Task #187 persistent user set), not that the camera is broken.

### 4.2 Exclusive access, kSA CLOSED

```powershell
python scripts\precheck_direct_camera.py --access-mode full --allow-dark-frame
```

RESULT — access mode: `__________`  verdict: `__________`

```powershell
python scripts\vimba_camera_smoke.py
```

RESULT (2026-08-05):

| | Ch-MBE | Bulbasaur (O-MBE) |
|---|---|---|
| Model | Manta_G-033B (E0022060) | Manta G-033B (E0022060) |
| Vimba ID | `DEV_000F314E8D67` | `DEV_000F314F7A86` |
| Interface | Ethernet 4 — `169.254.81.240` | Omicron Lab10 / instrument LAN |
| Bit depth | **12** (same model) | 12 |

✅ **Same camera model as Bulbasaur, so the hardcoded `bit_depth=12` /
`_max_value=4095` is correct on Ch-MBE.** Question #7 closes favourably — no
code change needed for normalization.

Two notes:

- `169.254.81.240` is an APIPA / link-local address — normal for a
  direct-attached GigE camera with no DHCP server, and Vimba enumerates by
  camera ID rather than IP so the address may change harmlessly. Expect slow
  discovery; this is exactly what `VmbCamera.CONNECT_TIMEOUT_S = 45.0`
  accommodates.
- `(E0022060)` appears on **both** chambers' cameras, so it is a kSA/model
  config code, **not** a unique unit serial. The units are distinguished by
  their `DEV_*` IDs above.

### 4.2b Exposure and gain — question #11

Now that Ch-MBE is Vimba-direct, this is the remaining classifier-input
risk. `VmbCamera` sets no exposure, gain, or pixel format; those come from
each camera's persistent user set. Record both chambers' values and compare.

```powershell
python -c "import vmbpy; from vmbpy import VmbSystem; s=VmbSystem.get_instance(); s.__enter__(); c=s.get_all_cameras()[0]; c.__enter__(); [print(f'{f.get_name():28} {f.get()}') for f in c.get_all_features() if f.get_name() in ('ExposureTime','ExposureAuto','Gain','GainAuto','PixelFormat','Width','Height','DeviceModelName','DeviceID')]"
```

RESULT — exposure: `__________`  gain: `__________`
pixel format: `__________`

> Bit depth matters: `VmbCamera` hardcodes `bit_depth=12` and derives
> `_max_value = 4095` from it, with `_create_camera` constructing
> `VmbCamera()` bare — no chamber can override it. If Ch-MBE is not a 12-bit
> Manta G-033B, direct-read normalization is wrong on this chamber.

### 4.3 kSAComm

```powershell
python scripts\test_ksa_comm.py
```

Confirms kSAComm still listens on 1800 after being enabled 2026-07-21. A kSA
reinstall or config reset would have dropped it.

RESULT — handshake: `__________`  protocol version: `______`

### 4.4 Palette reference frames

With kSA open, save one **screengrab** frame and one **Vimba** frame of the
same view into `$evidence\palette\`. The comparison against
`gui.ksa_palette.KSA_BGW_PALETTE` is Mac-side work, but only possible if the
frames are captured here.

The palette LUT is fixed and Bulbasaur-derived — it is *not* read from the
live kSA window, so the Vimba transform is chamber-independent. What is
unvalidated is whether Ch-MBE's kSA output agrees with that reference, and
whether Vimba and screengrab agree with each other on this chamber.

RESULT — frames saved? `____`

---

## Phase 5 — end-to-end GUI session

Fastest confirmation the GUI is genuinely using Ch-MBE configuration.

Keep camera and pyrometer in dummy mode if Phases 3–4 are complete; MISTRAL
in `ads`. Non-growth session.

```powershell
python growth_monitor_chmbe.py
```

Arm, collect ≥30 s, disarm. Note the session directory printed on start.

> The GUI takes the foreground and holds the shell until it exits. Open a
> second PowerShell window for the audit rather than closing it.

```powershell
python scripts\audit_session_sensor_log.py <session-folder>
```

Expected: `VERDICT: PASS`, all 56 ADS union columns present, cells 1–7
populated (no blank cell7, unlike Bulbasaur), Ch-MBE provenance in
`session_metadata.json`.

RESULT — verdict: `__________`  cells populated: `____/7`

---

## Do not run

- **`scripts\pyrometer_force_modbus.py`** — the only state-changing script in
  the repo. If discovery is silent in both RTS states, **stop and preserve the
  evidence**; do not enter the mode-switch ladder. The Jul 30 O-MBE session
  established that the ladder was built against a probe that was answering
  correctly throughout.
- Anything containing `--i-am-doing-a-write`.
- `scripts\test_elog.py` unchanged (O-MBE-specific `PANEL`).
- Any `scripts\test_*.py` file run indiscriminately — several are live
  hardware probes, not unit tests. `pytest.ini` excludes `scripts/` from
  collection for exactly this reason.

---

## Artifacts to preserve

Highest value first:

1. `chmbe_elog_schema.txt` — the Ch-MBE variable map
2. `pyro_discover_*` and `pyro_raw_*` bundles — both RTS states, raw bytes
3. `mistral_ads/` precheck JSON
4. Phase 5 session `sensor_log.csv` + `session_metadata.json`
5. Camera model / bit depth from 4.2
6. Palette frame pair from 4.4

---

## Post-lab

- Fill in every RESULT slot above, including failures — a recorded failure
  with its raw bytes is more useful than a blank.
- Config changes that follow from these results (`pyrometer_rts`,
  `pyrometer_port`, `evap_mode_default`, a Ch-MBE elog var map) belong on a
  branch with a PR, not a direct push to `main`.
- Update the Claude memory for the day's outcomes.
- Cross-reference any code changes to their commit hashes.
