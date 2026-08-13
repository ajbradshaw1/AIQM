# AIQM Software-Hardware Integration — Project Instructions

## Hardware Ports (Mac)
| Device | Port | Baud |
|--------|------|------|
| OWON PSU | `/dev/cu.usbserial-120` | 115200 (SCPI over USB serial) |
| Dracal TC | `/dev/cu.usbmodemE297161` | VCP mode |

## Two Operating Modes
1. **Normal Mode:** Human controls heater via standard PSU, watches RHEED/pyrometer on screen
2. **AI-Scientist Mode:** OWON PSU, RHEED → Classifier2 → RL → PID → OWON → heater (closed-loop)

## Key Scripts
| Script | Purpose |
|--------|---------|
| `owon_power_supply.py` | OWON SPE interface (SCPI) |
| `dracal_thermocouple_reader.py` | Dracal TC reader (VCP) |
| `temperature_pid_control.py` | PID loop: OWON + Dracal (uses `simple-pid` library) |
| `scripts/heater_step_test.py` | Step response test for PID tuning |
| `scripts/psu_diagnostic.py` | PSU connectivity check |
| `scripts/owon_self_test.py` | OWON self-test |

## Testing
Offline unit tests live in `tests/` and `tools/rheed_postprocessing_labeling/tests/` (run `pytest` from the repo root; `pytest.ini` limits collection to those paths). `scripts/` keeps the **hardware probe scripts** whose names also start with `test_` (`test_ads_read.py`, `test_elog.py`, `test_ksa_comm.py`, `test_ksa_single.py`, `test_mistral_jsonrpc_discovery.py`, `test_pyrometer.py`, plus `heater_step_test.py`, `owon_self_test.py`) — these talk to real instruments and exit at import on machines without them; never collect them with pytest.

### Mac: export the Qt plugin path before pytest
Without it the suite dies at the first `QApplication(...)` with a bare
`Fatal Python error: Aborted` and a C-level traceback — no Python exception,
no failing assertion. It looks exactly like a code regression and is not one.
Either form works:

```bash
export QT_QPA_PLATFORM_PLUGIN_PATH=/opt/anaconda3/lib/python3.13/site-packages/PyQt6/Qt6/plugins/platforms
```

```bash
export QT_PLUGIN_PATH=/opt/anaconda3/lib/python3.13/site-packages/PyQt6/Qt6/plugins
```

`QT_QPA_PLATFORM_PLUGIN_PATH` points at `platforms` itself; the broader
`QT_PLUGIN_PATH` points at its **parent**. Pairing the narrow variable with
the parent directory fails — verified on Qt 6.10.0 / PyQt 6.10.2. Same fix
as the documented `gui.py` launch export; it is needed for pytest too.

### Known pre-existing failures (measured 2026-08-12)

Neither is a regression. Both reproduce with no local changes present, so a
**third** failure is the one worth investigating.

1. `tests/test_weak_primary_shadow.py::CollectionDiscoveryTests::test_bundled_package_is_complete_and_is_the_default`
   — `ValueError: Release integrity validation failed` on the weak-primary
   encoder metadata. `MANIFEST.json` expects sha256 `941f9ede…` / 3,870 bytes;
   both committed copies are byte-identical at `fad75fc5…` / 3,793 bytes, and
   the expected hash matches no JSON blob in the tree. Fails on Linux CI too.
   Owned by the model bundle author — do not weaken the test or guess the
   manifest.
2. `tests/test_equalizer_alignment.py::CandidateTests::test_normal_candidate_ranks_first`
   — `AssertionError: 'mirrored' != 'normal'`, alongside divide-by-zero /
   overflow / invalid `RuntimeWarning`s from the `matmul` at
   `gui/equalizer_alignment.py:1238`. **Mac-only:** reproduces on `6ab8a1b`
   alone, while Linux CI on that same commit does not report it. Cause not
   established; a numerical-backend difference is the leading hypothesis.

Baselines for attribution: **Linux CI 989 passed / 1 failed / 1 skipped**;
**macOS local 1021 passed / 2 failed / 16 skipped** (counts differ because
`pytest.ini` also collects the labeler tests and the platform-gated skip sets
differ).

`tests/test_ocr.py` was moved to `scripts/` and is no longer collected; the
older note about it failing on Mac no longer applies.

## Two GUI Applications

This repo ships two distinct PyQt6 apps that share `gui/state.py`,
`gui/widgets.py`, and `gui/workers.py` but nothing else at the UI layer:

| Product | Launcher | Window title | Tabs | UI code |
|---|---|---|---|---|
| OMBE Growth Monitor | `python growth_monitor_app.py` | "OMBE Growth Monitor" | Monitor / Events / Session | `gui/growth_*.py`, `gui/events_tab.py`, `gui/auto_capture.py`, `gui/classifier_bridge.py` |
| Heater Control (dummy-loop) | `python gui.py` | "Hardware Control Dashboard" | RHEED / Pyrometer / Power Supply / Thermocouple / Dashboard / Visuals / Config / PID / Action Log | `gui/heater_control/` (subpackage) |

The growth monitor is the AI-MBE product (RHEED + pyrometer + auto-capture +
classifier integration). The heater-control GUI is the dummy-loop /
AI-Scientist-mode infrastructure (OWON PSU + Dracal TC + PID heater
control) — used for v4 closed-loop work when active. **Launch the right
one for your workflow — they're not the same product.**

## GUI (PyQt6) — OMBE Growth Log Assistant
Launch: `python growth_monitor_app.py`

**Current version (v2): Growth log assistant** — automates OMBE growth log data capture.
Reads RHEED from kSA 400 (screengrab) + pyrometer temp from TemperaSure (screengrab),
auto-timestamps grower notes, and exports partially-filled growth logs.

### Verified on Bulbasaur (Mar 23, 2026)
- **RHEED screengrab: WORKING** — captures detached kSA Live Video window
  - Requires kSA setting: Options → General → Images and Video → **uncheck "Keep Live Video inside application"**
  - Window found via 3-priority search: detached top-level → MDI child → main kSA fallback
- **Pyrometer screengrab: WORKING** — reads live temperature from TemperaSure (165°C at idle verified)
- **Full session test PASSED** — commit_log.csv, sensor_log.csv, session_metadata.json, growth_log.xlsx all generated correctly with correct data
- **Frame capture: WORKING** — RHEED PNGs saved to frames/ on each LOG ENTRY

### Layout
- **Top bar (always visible):** Grower name, Sample ID, ARM / START / STOP
- **Monitor tab:** Value displays (elapsed, temp, V, I), live RHEED image, expanding note text box, LOG ENTRY button (Ctrl+S)
- **Session tab:** Config (upper-left), Sensor Log interval reads (upper-right), Growth Notes grower commits (bottom half), Export Growth Log button

### Key Files
| File | Purpose |
|------|---------|
| `growth_monitor_app.py` | Launcher |
| `gui/growth_monitor.py` | Two-tab UI: Monitor (RHEED + notes) and Session (config + logs + export) |
| `gui/growth_app.py` | Orchestrator: ARM/DISARM/START/STOP, worker lifecycle, sensor logging, auto-export on STOP |
| `gui/growth_logger.py` | Session logging: sensor CSV, commit CSV, frame PNGs, JSON metadata, xlsx/CSV export |
| `gui/classifier_bridge.py` | Loads Classifier2 model — NOT used in v2, kept for v3 |
| `gui/auto_capture.py` | Auto-capture engine — NOT used in v2, kept for v3 |
| `gui/state.py` | Shared state dataclasses |
| `gui/workers.py` | Background worker threads (camera, pyrometer, PSU, thermocouple) |
| `gui/widgets.py` | Reusable widgets (ValueDisplay, ControlPanel, ProtectionPanel) |
| `drivers/rheed_camera.py` | RheedCamera ABC + DummyCamera, ScreenGrabCamera (3-priority kSA window search), VmbCamera |
| `drivers/pyrometer.py` | TemperatureSensor ABC + DummyPyrometer, ScreenGrabPyrometer, ModbusPyrometer |
| `drivers/config.py` | MBESystemConfig presets (OXIDE_MBE, CHALCOGENIDE_MBE) |

### Session Output
Each session creates a directory under the configured save folder containing:
- `sensor_log.csv` — periodic temperature readings at configured interval (default 10s)
- `commit_log.csv` — timestamped grower entries with notes, temp, V, I, frame paths
- `frames/` — RHEED frame BMPs captured at each LOG ENTRY (BMP matches Justin's training-data format)
- `session_metadata.json` — grower, sample ID, date, entry count
- `growth_log.xlsx` — OMBE growth log export (auto-generated on STOP, also via Export button)
- `temperature_profile.png` — auto-generated T-vs-t plot at STOP

Post-hoc analysis (via `scripts/growth_profile_explorer.py`) adds an `analysis/` subdirectory:
- `temperature_profile_annotated.png` — T-vs-t with commit / manual / auto-capture overlays
- `pyro_stability.png` — pyrometer mean ± σ band
- `classifier_trajectory.png` — classifier smoothed % per class over time
- `score_distribution.png` — auto-capture change_score histogram + per-state counts
- `grower_vs_classifier.png` — 5-panel scatter of grower slider % vs classifier %
- `growth_profile_report.html` — self-contained HTML wrapping all 5 with metadata (emailable, no external deps)

### Config Defaults
- Recording interval: 1s minimum, whole seconds (prevents faster-than-hardware commits)
- Camera mode: dummy (Mac) / screengrab (Bulbasaur)
- Pyrometer mode: dummy (Mac) / screengrab (Bulbasaur)

### ScreenGrabCamera — kSA Window Detection
The `ScreenGrabCamera` uses a 3-priority search to find the correct kSA window:
1. **Detached top-level window** — when kSA "Keep Live Video inside app" is unchecked (preferred)
2. **MDI child window** — searches inside the main kSA frame
3. **Main kSA window fallback** — captures the full application

Default search term: `"Live Video"`. Uses `EnumWindows` + `EnumChildWindows` with case-insensitive substring matching, excluding windows starting with "kSA 400" for priority 1.

### Known Issues / TODO
- RHEED capture includes kSA title bar + status bar — crop to image region only (cosmetic)
- Image very dim at idle (Pixel Int: 0.7%) — will be brighter during actual growth
- V/I displays show "---" — no source connected yet. Possible paths: kSA Temperature Control module (reads Eurotherm/ULVAC/Watlow via serial), kSA Analog Input Board (not currently installed), or direct from OWON in AI-Scientist mode (v4)
- Edge case testing needed: empty fields, rapid commits, multiple sessions, close-while-running

### Phased Roadmap
- **v1:** Classification-centric display + manual logging — **DONE** (archived)
- **v2 (current):** Growth log assistant for OMBE — **CORE FUNCTIONALITY VERIFIED**
  - [x] Python 3.12 on Bulbasaur, GUI launches
  - [x] RHEED screengrab captures detached kSA Live Video window
  - [x] Pyrometer screengrab reads live temp from TemperaSure (verified 165°C)
  - [x] Growth-log-centric UI with two-tab layout (Monitor + Session)
  - [x] OMBE growth log export (xlsx matching lab template + CSV fallback)
  - [x] Auto-export on STOP + manual Export button
  - [x] Full end-to-end growth session test on Bulbasaur — PASSED
  - [x] Sensor log: newest-on-top, no scroll reset, 10s interval reads verified
  - [x] Frame PNGs saved on each LOG ENTRY
  - [ ] Crop kSA title bar / status bar from RHEED capture
  - [ ] Edge case testing (empty fields, rapid commits, close-while-running)
  - [ ] Investigate kSA for V/I data (Temperature Control, Analog Input, Automation interface)
  - [ ] Test during actual growth (brighter RHEED image, real temperature ramps)
  - [ ] ChMBE variant (similar template, different elements)
- **v3:** Add reconstruction classification checkboxes + Classifier2 ML confidence
- **v4:** RL policy, PID loop, closed-loop AI-Scientist mode
- **Deferred:** OWON PSU on Bulbasaur, PID step tests, auto-capture

## PID Control (updated Mar 26, 2026)
**Library:** `simple-pid` (pip install simple-pid) — replaces custom PID math in both:
- `temperature_pid_control.py` (CLI) and `gui/pid_controller.py` (GUI)
- Same `RobustPID` wrapper interface. All safety code (cutoffs, slew limits, fault states) unchanged.

**Dummy loop status:** COMPLETED as proof of concept. Software stack validated.
**Gains do NOT transfer to MBE** — different thermal dynamics. Retune on MBE when ready.

**Key lesson:** Safety constraints must target the RIGHT variable. Voltage slew rate limiting (0.5V/s) prevented PID from cutting power fast enough → 32°C overshoot (targeted 60°C, hit 92.5°C). The hard temperature cutoff is the real safety net. Increase `--max-voltage-step` to allow responsive control.

**Recommended next test:**
```
python temperature_pid_control.py --target 60 --hold-minutes 2 \
  --kp 0.3 --ki 0.02 --kd 0.0 --max-voltage 10 --max-temp-hard 150 \
  --max-temp-rate 300 --max-voltage-step 2.0 --no-prompt
```

**Hardware limits (actual):** Heater=400°C, ceramic boat=600°C, high-temp tape=250-300°C (limiting factor), OWON=24V/1A.

**GUI launch (Mac):**
```
export QT_QPA_PLATFORM_PLUGIN_PATH=/opt/anaconda3/lib/python3.13/site-packages/PyQt6/Qt6/plugins/platforms
python gui.py
```

## kSA 400 Configuration (Bulbasaur)
- **Camera:** AVT Manta_G-033B (E0022060) 10, model AVT Vimba
- **Live Video title:** `AVT Manta_G-033B (E0022060) 10 Live Video [live]`
- **Settings:** Exposure 700ms, Sum 1, Palette Green scale, Mapping Normal, A/D Black=30 White=0 Gain=1.0
- **Critical setting:** Options → General → Images and Video → "Keep Live Video inside application" must be **unchecked**
- **Chamber Interface:** Set to `<None>`, options available: kSAModBus32, kSAComm, Automation, kSADigitalControl
- **User manual:** `C:\Users\Lab10\Documents\kSA\kSA 400\Help\` (PDF + HTML)

### kSA Data Paths (from manual analysis, Mar 30 2026)
- **Auto Export Data:** Enable in Advanced Acquisition Options > Document Generation > "All data to text." kSA writes ASCII `.txt` alongside `.kdt` for every acquisition. Our GUI can file-watch the Output directory.
- **Temperature Control Module:** Natively reads Eurotherm/ULVAC/Watlow controllers via serial. Shows temp, setpoint, output power (%). Records in `.kdt`. Need to check if OMBE heater controller is compatible.
- **Chamber Interface TCP/IP:** Under-documented in user guide. Options likely enable Modbus-TCP, proprietary, or automation protocol. Need docs from k-Space.
- **Phase-Locked Epitaxy (PLE):** Built-in closed-loop RHEED oscillation → shutter control. Relevant to v4 RL design.
- **Image Batch Export:** Can export RHEED frames as lossless TIFF/PNG — better than screengrab for ML training data.
- **Analog Input Board:** Supported but NOT installed on Bulbasaur. No analog input module in lab.
- **Key directories:** Data at `C:\Users\Lab10\Documents\kSA\kSA 400\Data\`, Logs at `...\Logs\`

## Pyrometer Direct Access (from manual analysis, Mar 30 2026)
- **Direct Modbus RTU:** Default mode on power-up. COM4, 115200/8N1, slave 0x01. Temp at regs 0x0000-0x0001 (IEEE-754 float big-endian). Current at 0x0004-0x0005. ~10 rd/s polling.
- **TemperaSure switches pyrometer out of Modbus** into proprietary Exactus Protocol. Cannot run both simultaneously. Power-cycle probe to return to Modbus.
- **No analog output module (IFA-5) in lab** — only digital IFD-5.
- **TemperaSure Macro Mode:** Auto-exports CSV to `C:\BASF\Macro Data`. File-watch option that keeps TemperaSure running.
- **v3 plan:** Replace TemperaSure with direct Modbus reads from Python. Requires building pyrometer display in Growth Monitor.

### Lab Visit Checklist (Next Bulbasaur Session)
- [ ] kSA: Enable "Auto Export Data" > "All data to text", run acquisition, check `.txt` output
- [ ] kSA: Try each Chamber Interface option, note behavior
- [ ] kSA: Check View > Temperature Control — is it configured?
- [ ] kSA: Check existing data in `...\Data\` and `...\Logs\` directories
- [ ] kSA: Batch export saved RHEED frames as TIFF, compare vs screengrab quality
- [ ] TemperaSure: Set up Macro Mode auto-export, check file format/timing
- [ ] Pyrometer: Close TemperaSure, power-cycle probe, test direct Modbus read via pymodbus
- [ ] Identify exact Eurotherm model on OMBE (check front panel — likely 3508)
- [ ] Find Eurotherm IP on instrument subnet (try `10.0.42.x` range or check MISTRAL config)
- [ ] Test Modbus TCP read from Bulbasaur: `python -c "from pymodbus.client import ModbusTcpClient; c=ModbusTcpClient('IP',502); c.connect(); print(c.read_holding_registers(1,1).registers[0]/10)"`
- [ ] Research kSA Chamber Interface TCP/IP documentation (k-Space Associates — email requestinfo@k-space.com, or ask lab members for kb.k-space.com credentials)

## MBE Hardware Control
- **Scienta Omicron MISTRAL** controls all MBE hardware (pumps, valves, heaters, manipulators) via touch-screen panels. GUI-based, no serial API. Not needed for v2.
- **Eurotherm Temperature Controller** (likely model 3508) — controls substrate heater. Speaks **Modbus TCP on port 502**. IP likely on instrument subnet (`10.0.42.x` or `10.120.40.170`). Python driver lives in-repo at `drivers/temp_pid.py` (extracted from the retired research-lab repo). Key registers: PV temp (reg 1, /10), setpoint (reg 2), current SP (reg 5), output power rate (reg 36). Safety limits 20-930°C. **This is how we get V/I and heater temp into the Growth Monitor — over Ethernet, no port conflicts.**
- **OWON PSU** is our controllable heater bypass for AI-Scientist mode (v4). Deprioritized per PI.
- CPU inference benchmarked at **13ms/frame** — no GPU/cluster needed.
- **Note:** Eurotherm is now owned by Watlow (acquisition completed Oct 2022). kSA 400's support for both "Eurotherm" and "Watlow" covers the same product line.

## Deployment Target
- **Bulbasaur** (lab PC, Windows): Python 3.12.10 installed, GUI verified 2026-03-23
  - User: `Lab10`
  - Displays: 1920x1200 + 1920x1080
  - COM4: Prolific PL2303GS → pyrometer RS-422 (IFD-5)
  - OWON PSU: not yet connected on Bulbasaur (deprioritized)
  - TeamViewer: ID 1 756 871 318

## Setup
```bash
# Mac
python3 -m venv .venv && source .venv/bin/activate
pip install pyvisa pyvisa-py pyserial PyQt6 pyqtgraph numpy

# Bulbasaur (Windows) — no venv, system Python
pip install PyQt6 pyqtgraph numpy pyvisa pyvisa-py pyserial mss Pillow pywinauto pymodbus openpyxl
```
