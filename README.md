# AIQM Software-Hardware Integration

PyQt6 software stack for the Yang Group's AI-driven MBE growth experiments.
Two distinct applications live in this repository — see
[Two GUI Applications](#two-gui-applications) below.

## Installation

For either live Windows acquisition environment:

```powershell
python -m pip install -r requirements-windows-live.txt
```

The overlay pins the WGC package used by `screengrab`. Vimba direct-camera
mode still requires the vendor Vimba X SDK and its matching `vmbpy` package.
Classifier2 has its own PyTorch/checkpoint installation requirements.

### O-MBE and Ch-MBE repository double-click launch

On the matching workstation, double-click `Start O-MBE Growth Monitor.cmd` or
`Start Ch-MBE Growth Monitor.cmd` in the repository root. Each launcher forces
and preflights its own chamber profile, starts from the repository root, and
writes diagnostics under `%LOCALAPPDATA%\AI4MBE\LauncherLogs`.

This launcher does not install packages, change persistent environment
variables, alter instrument settings, or choose acquisition modes. Before
ARM/START, the operator must still verify every live mode and the save folder.
The selected Python must import the production-default camera, ADS, and serial
drivers; O-MBE additionally requires `pymodbus`. If no candidate passes, set
`AI4MBE_GUI_PYTHON` to the full path of the validated `ai4mbe-gui` Python and
double-click the launcher again.

## Two GUI Applications

| Product | Launch | Window title | Tabs |
|---|---|---|---|
| **O-MBE Growth Monitor** | double-click `Start O-MBE Growth Monitor.cmd` | "Oxide MBE Growth Monitor" | Monitor / Direct-read / Events / Scrubber / Live Equalizer / Session |
| **Ch-MBE Growth Monitor** | double-click `Start Ch-MBE Growth Monitor.cmd` | "Chalcogenide MBE Growth Monitor" | Monitor / Direct-read / Events / Scrubber / Live Equalizer / Session |
| **Hardware Control Dashboard** (dummy-loop heater control) | `python gui.py` | "Hardware Control Dashboard" | RHEED / Pyrometer / PSU / Thermocouple / Dashboard / Visuals / Config / PID / Action Log |

The two apps share only `gui/state.py`, `gui/widgets.py`, and
`gui/workers.py`. The growth monitor is what runs during actual growths;
the heater-control dashboard is v4 closed-loop infrastructure for the
AI-Scientist mode roadmap.

## OMBE Growth Monitor — what it does

Automates the growth-log workflow during an MBE growth session:

- **Heartbeat capture** — saves a RHEED frame every 5 s by default
  (env-tunable via `AIQM_HEARTBEAT_INTERVAL_SECONDS`).
- **Sensor log** — 1 Hz CSV of pyrometer temperature, MISTRAL V/I,
  chamber pressure, and (in `elog` mode) substrate manipulator
  temperature + active cell PVs + plasma source state.
- **Auto-capture** — pixel-diff change detector flags RHEED frame
  buffers around detected reconstruction transitions.
- **Commit log** — timestamped grower notes via the LOG ENTRY button
  capture the moment with the current sensor snapshot.
- **RHEED view and image-usability log** — explicit gun-alignment boundaries,
  camera-history resets, and one-frame records of whether an acquired image
  can be analyzed. These records do not describe surface or film quality.
- **Growth-log export** — auto-generated `growth_log.xlsx` at session
  end.

### Direct-read instrument modes

The growth monitor supports multiple data-source modes per channel.
Configure in the Session tab → Config form before ARM/START:

| Channel | Modes |
|---|---|
| RHEED camera | `vimba` (vmbpy SDK, bypasses kSA) / `screengrab` (WGC reads detached kSA Live Video by HWND) / `screengrab_mss` (legacy diagnostic) / `dummy` |
| Pyrometer | `modbus` (chamber COM/baud/RTS/device/backend) / `exactus` (binary serial alternative) / `screengrab` (TemperaSure UI) / `dummy` |
| EvapControl | `elog` (parses EvapControl's own `.elo` binary log directly) / `screengrab` (OCR) / `dummy` |
| MISTRAL | `ads` (read-only chamber-specific TwinCAT endpoint) / `screengrab` (OCR) / `jsonrpc` (experimental, may return no values) / `dummy` |

Both chamber profiles start on `vimba / modbus / ads / elog`. O-MBE uses COM4
with the `pymodbus` backend and a 6-cell ADS profile. Ch-MBE uses COM3 with the
`raw_serial` backend and a 7-cell ADS profile. Both verified adapters require
RTS de-asserted. The GUI passes a chamber-specific EvapControl log directory;
it does not use cross-chamber path auto-detection during production startup.

WGC RHEED capture is independent of desktop z-order, so covering or moving
the detached Live Video window does not contaminate the image. It fails closed
if the window is minimized, closed, or stops producing frames. OCR screengrabs
remain monitor-pixel based; direct-read modes also avoid their positioning and
OCR mis-read failure modes.

### Output per session

Each session creates a directory containing:
- `sensor_log.csv` — 1 Hz sensor readings
- `commit_log.csv` — grower LOG ENTRY records with attached frame paths
- `auto_capture_events.csv` — detector-flagged events with buffer dumps
- `manual_events.csv` — one-click grower event marks
- `heartbeat_log.csv` — periodic captures with camera, timing, geometry,
  view-state, and accepted Equalizer calibration provenance when available
- `rheed_view_events.csv` — alignment, visual generation, history, and
  explicit `qc_pass`/`qc_reject` image-usability event names retained for
  file compatibility
- `rheed_event_revisions.jsonl` — append-only point-event review history
- `rheed_point_events.json` — rebuildable current point-event state
- `frames/` — RHEED frame PNGs (heartbeat + per-event buffers)
- `session_metadata.json`
- `growth_log.xlsx` (auto-generated on STOP)

Rows that save a RHEED image include `capture_backend`, `captured_at_utc`,
`capture_sequence`, `frame_age_ms`, and `source_hwnd`. The capture timestamp
is when Python received the WGC frame, not the camera exposure time; it supports
software alignment but is not hardware synchronization.

## Repository layout

```
gui/                          OMBE Growth Monitor UI + workers
  growth_app.py               Top-level orchestrator
  growth_monitor.py           Monitor / Events / Session tab widget
  growth_logger.py            Session CSV/PNG writers
  events_tab.py               Auto-capture event banner + review UI
  auto_capture.py             Change-detection engine
  classifier_bridge.py        Classifier2 model integration
  workers.py                  Background threads per channel
  state.py                    Shared state dataclasses
  widgets.py                  Reusable widget primitives
  heater_control/             Hardware Control Dashboard (separate app)

drivers/                      Instrument drivers
  rheed_camera.py             VmbCamera / ScreenGrabCamera / DummyCamera
  pyrometer.py                ModbusPyrometer / ExactusSerialPyrometer /
                              ScreenGrabPyrometer / DummyPyrometer
  evap_control.py             ElogReader / EvapControl (OCR) / DummyEvapControl
  mistral.py                  MistralGui (OCR) / DummyMistralGui
  elog.py                     .elo binary-log parser for EvapControl
  frame_quality.py            Black/saturation/uniform frame rejector
  ocr.py                      Tesseract wrappers
  config.py                   MBESystemConfig presets (OXIDE_MBE, etc.)

scripts/                      CLI utilities, smoke tests, validation reports
  find_mistral_logs.py        Search Bulbasaur for MISTRAL log files
  test_ksa_single.py          kSA TCP/IP wire-protocol probe
  test_elog.py                Smoke test for the .elo parser
  rheed_change_detector*.py   Offline detector + HTML validation report
  plot_temperature.py         Post-session T vs t plot from sensor_log.csv
  vimba_*.py                  Allied Vision direct-camera demos
  equalizer_*.py              Hybrid-basis labeling-game prototype
  ...

tools/rheed_postprocessing_labeling/
  README.md                   Offline point-event workflow and safety boundary
  desktop_launcher.py         Windows build/open/validate application
  point_events.py             Point-event import, revision, and validation
  loopback_service.py         Token-protected 127.0.0.1 report service
  templates/timeline.html     Interactive timeline and Unfinished queue
  tests/                      Synthetic offline unit and browser tests

reference/                    Schema dumps, manuals, instrument datasheets
docs/                         Methods writeups, schema proposals, deck artifacts
f_version/                    Frankie Moreno's parallel intensity-work GUI
```

## Setup

Bulbasaur (lab PC) uses Python 3.12 with no venv — packages installed
system-wide. For Mac development:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install PyQt6 pyqtgraph numpy Pillow pymodbus pyserial pytesseract \
            vmbpy simple-pid
```

Run the growth monitor in dummy mode (no hardware needed):

```bash
python growth_monitor_app.py
# Session tab → Config form: set all modes to "dummy"
# Top bar: enter Grower name + Sample ID → ARM → START
```

## Post-hoc analysis tools

Session directories under `logs/growths/` accumulate CSVs + frames as
a session runs. These CLI scripts turn those artifacts into
diagnostic charts and reports after the fact — no lab PC required.

| Script | Output | Use |
|---|---|---|
| `scripts/plot_temperature.py` | Single T-vs-t PNG | Quick temperature-trace view of one session |
| `scripts/growth_profile_explorer.py` | 5 PNGs + self-contained HTML report in `<session>/analysis/` | Full session review: T + std band + event overlays + classifier trajectory + auto-capture score distribution + grower-vs-classifier agreement scatter. HTML wraps all 5 with base64-embedded PNGs and a session metadata header — emailable, no external dependencies |
| `scripts/validate_angle_robustness.py` | HTML report + CSV | Classifier sensitivity to camera-angle rotations against an archived session |
| `tools/rheed_postprocessing_labeling/` | Interactive local report + JSON/CSV sidecar | Review manual, automatic-change, and posthoc RHEED point events; run exact-frame Equalizer through the desktop launcher; preserve legacy interval annotations as read-only |

```bash
# Five-chart + HTML report
python scripts/growth_profile_explorer.py \
    logs/growths/growth_Group-Test_20260710_161714/
# → writes analysis/{temperature_profile_annotated,pyro_stability,
#   classifier_trajectory,score_distribution,grower_vs_classifier}.png
# and analysis/growth_profile_report.html (open in browser)

# Custom output directory + DPI
python scripts/growth_profile_explorer.py \
    logs/growths/<session_dir>/ \
    --output-dir /tmp/analysis --dpi 200

# Long session — subsample sensor + heartbeat rows every N
python scripts/growth_profile_explorer.py \
    logs/growths/<session_dir>/ --stride 4

# PNGs only, skip HTML assembly
python scripts/growth_profile_explorer.py \
    logs/growths/<session_dir>/ --no-html
```

## Hardware (O-MBE / Bulbasaur lab PC)

| Device | Connection | Notes |
|---|---|---|
| RHEED camera | Allied Vision Manta G-033B (GigE Vision) | Vimba SDK or screengrab kSA Live Video |
| Pyrometer | BASF Exactus + IFD-5 | COM4 / 115200 / 8N1, slave ID 1 (Modbus) or 5-byte binary serial |
| MISTRAL | Scienta Omicron / Beckhoff PLC | Read-only ADS profile at the O-MBE endpoint; OCR remains optional |
| EvapControl | Scienta Omicron — `evap_control.exe` | Direct: `.elo` binary log at `C:\_Omicron_Software\EvapControl\...\log\` |
| Chamber pressure gauge | Thyracont via Moxa NPort 5150 | Direct path scoped, not yet implemented |

## Project context

Built for the Yang Group at the University of Chicago Pritzker School of
Molecular Engineering. The MBE setups (OMBE, ChMBE, Cloud MBE) produce
quantum-material thin films; the AI-Scientist-mode roadmap eventually
closes the loop between RHEED-derived reconstruction classification and
substrate temperature control. This GUI is the primary data-collection
surface for that work.

See `CLAUDE.md` for the operational instructions used by Claude Code
sessions on this repo.
