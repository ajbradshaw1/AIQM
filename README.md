# AIQM Software-Hardware Integration

PyQt6 software stack for the Yang Group's AI-driven MBE growth experiments.
Two distinct applications live in this repository — see
[Two GUI Applications](#two-gui-applications) below.

## Install the Windows x64 Release

1. Open this repository's
   [GitHub Releases page](https://github.com/AaravSonthalia/AIQM-Software-Hardware-Integration/releases).
2. Under the required release, download the asset named
   `AI4MBE-Growth-Monitor-<version>-Windows-x64-Setup.exe`.
   **Do not download** GitHub's automatically generated `Source code (zip)` or
   `Source code (tar.gz)` files; those are not the workstation installer.
3. Double-click the downloaded `Setup.exe` and choose the program installation
   folder in the Windows setup wizard.
4. After installation, use the **O-MBE Growth Monitor** or
   **Ch-MBE Growth Monitor** Desktop shortcut for the correct instrument.

The installer supports **64-bit Windows and 64-bit Python only**. It installs
without administrator privileges when the selected location is user-writable,
reuses a compatible `ai4mbe-gui` environment when available, or creates an
isolated fallback environment. The folder picker initially suggests
`%LOCALAPPDATA%\Programs` but another writable drive or folder can be selected.
Experiment sessions are stored separately under
`Documents\AI4MBE\GrowthSessions` and are preserved by the uninstaller.
Before START, their location can be changed independently inside the GUI at
**Session → Config → Save folder → Browse**.

The Release ZIP remains available only as a fallback portable package. Its
`.cmd` installer is not the standard Windows setup experience.

Full installation, update, and uninstall details are in the
[Windows one-click installation guide](docs/WINDOWS_ONE_CLICK_INSTALL.md).

### Operator manual

- On GitHub: [English PDF operator manual](docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf)
- Searchable on GitHub: [English Markdown manual](docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.md)
- Earlier edition: [bilingual PDF manual](docs/RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf)
- After installation: use the **AI4MBE Operator Manual** Desktop shortcut, or
  open `docs\RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf` below the
  installation folder you selected.

The Release ZIP includes these manuals; they do not require a separate
download.

### Developer/live-driver installation

For Bulbasaur's live Windows acquisition environment:

```powershell
python -m pip install -r requirements-windows-live.txt
```

The overlay pins the WGC package used by `screengrab`. Vimba direct-camera
mode still requires the vendor Vimba X SDK and its matching `vmbpy` package.
Classifier2 has its own PyTorch/checkpoint installation requirements.

## Double-click launch on Windows

For a developer clone, double-click
`Install AI4MBE Desktop Shortcuts.cmd` once. It creates five desktop shortcuts:

- **O-MBE Growth Monitor** starts the live acquisition GUI with the chamber
  fixed to O-MBE and prevents a second competing O-MBE instance.
- **Ch-MBE Growth Monitor** starts the live acquisition GUI with the chamber
  fixed to Ch-MBE and prevents a second competing Ch-MBE instance.
- **RHEED Post-processing Labeler** opens a file-selection window for building,
  reviewing, and validating offline temporal labels.
- **AI4MBE Operator Manual** opens the English PDF guide.
- **Uninstall AI4MBE Growth Monitor** removes program files and shortcuts while
  preserving experiment sessions and launcher logs.

The launcher discovers the existing `ai4mbe-gui` interpreter, starts from the
repository root, and writes diagnostics under
`%LOCALAPPDATA%\AI4MBE\LauncherLogs`. It never installs packages or changes
instrument settings. The two root-level `Start *.cmd` files can also be
double-clicked directly. See
[`docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf`](docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf)
for the English operator workflow and troubleshooting guide. A searchable
[Markdown edition](docs/RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.md)
and a constrained
[AI prompt pack](docs/RHEED_GUI_Postprocessing_Labeling_AI_Prompt_Pack_EN.md)
are provided alongside it. The guide covers the shared workflow once and
branches explicitly where O-MBE and Ch-MBE differ; every numbered chapter
begins with an `In brief` summary. The earlier
[bilingual edition](docs/RHEED_GUI_Postprocessing_Labeling_User_Manual.pdf)
remains available.

## Two GUI Applications

| Product | Launch | Window title | Tabs |
|---|---|---|---|
| **O-MBE Growth Monitor** (primary product) | double-click `Start O-MBE Growth Monitor.cmd` | "Oxide MBE Growth Monitor" | Monitor / Events / Session |
| **Ch-MBE Growth Monitor** | double-click `Start Ch-MBE Growth Monitor.cmd` | "Chalcogenide MBE Growth Monitor" | Monitor / Events / Session |
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
- **RHEED view/QC log** — explicit gun-alignment boundaries, camera-history
  resets, and one-frame acquisition-QC labels without deleting frames.
- **Growth-log export** — auto-generated `growth_log.xlsx` at session
  end.

### Direct-read instrument modes

The growth monitor supports multiple data-source modes per channel.
Configure in the Session tab → Config form before ARM/START:

| Channel | Modes |
|---|---|
| RHEED camera | `vimba` (vmbpy SDK, bypasses kSA) / `screengrab` (WGC reads detached kSA Live Video by HWND) / `screengrab_mss` (legacy diagnostic) / `dummy` |
| Pyrometer | `modbus` (Modbus RTU — O-MBE COM4, Ch-MBE COM3; the port comes from the chamber config, not a fixed default) / `exactus` (binary serial alternative) / `screengrab` (TemperaSure UI) / `dummy` |
| EvapControl | `elog` (parses EvapControl's own `.elo` binary log directly) / `screengrab` (OCR) / `dummy` |
| MISTRAL | `screengrab` (OCR) / `dummy` — no direct-read driver yet |

WGC RHEED capture is independent of desktop z-order, so covering or moving
the detached Live Video window does not contaminate the image. It fails closed
if the window is minimized, closed, or stops producing frames. OCR screengrabs
remain monitor-pixel based; direct-read modes also avoid their positioning and
OCR mis-read failure modes.

For the direct Vimba camera, the Session tab also provides a manual exposure
setting in milliseconds. It is applied on **ARM**, verified by camera
readback, and recorded in `session_metadata.json`. The setting is volatile
(the application never saves a camera user set), requires Full camera access
and `ExposureAuto=Off`, and is locked with the other hardware configuration
while armed or running. Select **Keep current** to perform no exposure write.

### Output per session

Each session creates a directory containing:
- `sensor_log.csv` — 1 Hz sensor readings
- `commit_log.csv` — grower LOG ENTRY records with attached frame paths
- `auto_capture_events.csv` — detector-flagged events with buffer dumps
- `heartbeat_log.csv` — periodic-capture index
- `rheed_view_events.csv` — alignment, visual generation, history, and
  explicit `qc_pass`/`qc_reject` events
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
  events_tab.py               Auto-capture event banner + labeling UI
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

tools/
  rheed_postprocessing_labeling/  Offline model timelines + temporal segment annotation

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
| `python -m tools.rheed_postprocessing_labeling desktop` | Offline interactive HTML + JSON/CSV labels | Open the desktop report builder and validator; see `tools/rheed_postprocessing_labeling/README.md` |

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

## Hardware (Bulbasaur lab PC)

| Device | Connection | Notes |
|---|---|---|
| RHEED camera | Allied Vision Manta G-033B (GigE Vision) | Vimba SDK or screengrab kSA Live Video |
| Pyrometer | BASF Exactus + IFD-5 | COM4 / 115200 / 8N1, slave ID 1 (Modbus) or 5-byte binary serial |
| MISTRAL | Scienta Omicron — `MistralGui.exe` | No direct API; screengrab + OCR for now |
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
