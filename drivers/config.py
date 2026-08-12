"""
System-specific configurations for Oxide MBE and Chalcogenide MBE setups.

Each MBE system has different:
  - TemperaSure window titles
  - TemperaSure exe paths
  - kSA window titles
  - COM ports for Modbus pyrometer
  - Data storage paths
  - MISTRAL / EvapControl driver mode defaults
  - Effusion cell display labels

Select the active config with ``get_active_config()``, which reads the
``AIQM_CHAMBER`` environment variable (default: ``"ombe"``). The two
chamber-specific launcher scripts set this before the app starts:

    growth_monitor_ombe.py   → AIQM_CHAMBER=ombe
    growth_monitor_chmbe.py  → AIQM_CHAMBER=chmbe
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MBESystemConfig:
    """Configuration for a specific MBE system."""

    name: str

    # Chamber identifier — consumed by get_active_config() and the GUI
    # to branch on chamber-specific behaviour.
    chamber_id: str = "ombe"

    # Default driver mode selections for the Session config panel.
    # The GUI uses these as setCurrentText() seeds; the grower can still
    # override them manually before arming.
    camera_mode_default: str = "vimba"
    pyrometer_mode_default: str = "modbus"
    mistral_mode_default: str = "screengrab"
    evap_mode_default: str = "elog"

    # EvapControl log directory passed to ElogReader.
    # Empty string = ElogReader uses its built-in multi-path auto-detect.
    evap_log_dir: str = ""

    # Effusion cell display entries for the Direct-read tab.
    # Each dict:
    #   "label"       — display name shown in the ValueDisplay widget
    #   "state_field" — EvapControlState attribute name to read; None
    #                   when the cell is not elog-sourced (e.g. Ch-MBE
    #                   cells are fed from ADS instead)
    cell_display: list = field(default_factory=lambda: [
        {"label": "HTEC2",       "state_field": "cell_HTEC2_pv_C"},
        {"label": "Y (Yttrium)", "state_field": "cell_Y_pv_C"},
        {"label": "Sr",          "state_field": "cell_Sr_pv_C"},
        {"label": "Eu",          "state_field": "cell_Eu_pv_C"},
        {"label": "Er",          "state_field": "cell_Er_pv_C"},
    ])

    # kSA 400 RHEED window title (for screen scraping)
    ksa_window_title: str = "AVT Manta_G Live Video"

    # TemperaSure pyrometer window title
    temperasure_title: str = "BASF TemperaSure 5.7.0.4 Advanced Mode"

    # TemperaSure executable path (for auto-start)
    temperasure_exe: str = ""

    # Modbus pyrometer serial port
    pyrometer_port: str = "COM4"
    pyrometer_baudrate: int = 115200
    pyrometer_device_id: int = 1
    # RTS line state applied immediately after the port opens.
    #
    #   None  — make no claim; leave whatever pyserial does by default.
    #   True  — assert RTS.
    #   False — de-assert RTS.
    #
    # This is a property of the CABLING AND ADAPTER, not of the probe, so
    # it belongs per chamber rather than as a driver default. On Bulbasaur
    # (O-MBE: IFD-5 in RS232 position + Prolific PL2303GS) an asserted RTS
    # — pyserial's default — puts the link into loopback, returning every
    # request byte-for-byte while the probe never sees it. Measured on
    # COM4, Jul 30 2026:
    #
    #   RTS=True  -> 01 03 13 00 00 01 80 8E  (the request, echoed)
    #   RTS=False -> 01 03 02 09 03 FE 15     (version 9.3, CRC valid)
    #
    # DTR had no effect on either outcome and is deliberately not
    # configured here.
    #
    # The default is None rather than False ON PURPOSE. Setting False for
    # a chamber whose cabling nobody has tested is a behaviour change on
    # that system, not a configuration entry — and it would silently
    # export an O-MBE finding to hardware it was never measured against.
    # None preserves whatever that chamber did before, and connect()
    # logs a hint naming this field when it sees None, so an unconfigured
    # chamber diagnoses itself the first time someone looks at the log.
    pyrometer_rts: Optional[bool] = None
    # Modbus transport implementation. ``pymodbus`` remains the safe
    # default; ``raw_serial`` is a read-only CRC-scanning path for adapters
    # whose replies pymodbus cannot frame reliably.
    pyrometer_modbus_backend: str = "pymodbus"

    # Data storage paths
    single_images_folder: str = ""
    stream_images_folder: str = ""

    # Camera settings
    camera_index: int = 0
    camera_fps: float = 1.0

    # MISTRAL ADS backend config — per-chamber Beckhoff PLC endpoint.
    # Empty ads_netid disables the "ads" MistralWorker mode for the chamber.
    # ads_port_main / ads_port_pid follow the TwinCAT convention: 851 for
    # PLC Task 1 (Main.*), 852 for PLC Task 2 (PIDProgram.*).
    # ads_display_confirmed gates whether the existing _cell_displays
    # widgets get populated from ADS Cell{i}_T. Set False when the
    # cell_display uses material labels whose Cell{N} → material mapping
    # is not yet confirmed — ADS data still flows to CSV in that case.
    ads_netid: str = ""
    ads_port_main: int = 851
    ads_port_pid: int = 852
    ads_cell_count: int = 7
    ads_display_confirmed: bool = False


# ---------------------------------------------------------------------------
# Pre-configured systems
# ---------------------------------------------------------------------------

OXIDE_MBE = MBESystemConfig(
    name="Oxide MBE",
    chamber_id="ombe",
    # ads mode validated Jul 27 2026 (direct pyads to PLC 10.0.42.111.1.1).
    # screengrab still available as fallback via the sidebar dropdown.
    mistral_mode_default="ads",
    evap_mode_default="elog",
    # evap_log_dir left empty — ElogReader auto-detects the Bulbasaur path
    cell_display=[
        {"label": "HTEC2",       "state_field": "cell_HTEC2_pv_C"},
        {"label": "Y (Yttrium)", "state_field": "cell_Y_pv_C"},
        {"label": "Sr",          "state_field": "cell_Sr_pv_C"},
        {"label": "Eu",          "state_field": "cell_Eu_pv_C"},
        {"label": "Er",          "state_field": "cell_Er_pv_C"},
    ],
    temperasure_title="BASF TemperaSure 5.7.0.4 Advanced Mode",
    temperasure_exe=r"C:\Users\Lab10\Desktop\TemperaSure.exe",
    # VERIFIED on hardware Jul 30 2026 — probe EXI4765 answered
    # REG_VER 0x1300 with version 9.3, matching TemperaSure's reported
    # FW 9.3.0.6, and REG_CH1_TEMP with 201.41 C against TemperaSure's
    # 201.5. First direct read of this probe.
    pyrometer_rts=False,
    single_images_folder=(
        r"C:\Users\Lab10\Desktop\Automated RHEED Image Acquisition"
        r"\Acquiring Images Via Python Script Tests\Single Images"
    ),
    stream_images_folder=(
        r"C:\Users\Lab10\Desktop\Automated RHEED Image Acquisition"
        r"\Acquiring Images Via Python Script Tests\Stream Images"
    ),
    # ADS: 6 cells on Bulbasaur (Cell7 raises symbol-not-found).
    # ads_display_confirmed=False because cell_display uses material
    # labels (Sr, Eu, Er, etc.) and the ADS Cell{N} → material mapping
    # is still unconfirmed — ADS data flows to CSV only until Jiangang
    # confirms the physical wiring.
    ads_netid="10.0.42.111.1.1",
    ads_cell_count=6,
    ads_display_confirmed=False,
)

CHALCOGENIDE_MBE = MBESystemConfig(
    name="Chalcogenide MBE",
    chamber_id="chmbe",
    mistral_mode_default="ads",
    # Direct-read the chamber's own .elo file. Ch-MBE cell temperatures
    # continue to come from ADS; ElogReader safely leaves schema fields that
    # are absent from this chamber blank.
    evap_mode_default="elog",
    evap_log_dir=r"C:\evap_control_1.2.0.48\log",
    # Cell1 = manipulator (substrate heater — confirmed Jul 22 2026).
    # Cell2–7 physical mapping (Fe/Se/Te cracker) pending Jiangang
    # confirmation. state_field=None: these come from ADS, not elog.
    cell_display=[
        {"label": "Cell1 (Substrate)", "state_field": None},
        {"label": "Cell2",             "state_field": None},
        {"label": "Cell3",             "state_field": None},
        {"label": "Cell4",             "state_field": None},
        {"label": "Cell5",             "state_field": None},
        {"label": "Cell6",             "state_field": None},
        {"label": "Cell7",             "state_field": None},
    ],
    temperasure_title="BASF TemperaSure 5.7.0.4",
    temperasure_exe=r"C:\Users\Omicron\Desktop\TemperaSure.exe",
    pyrometer_port="COM3",
    pyrometer_baudrate=115200,
    # VERIFIED on Ch-MBE COM3, Aug 5 2026: the read-only raw probe returned
    # firmware 9.3 and 213.84056 C with RTS=False. pymodbus 3.14 timed out on
    # the same link, so this chamber uses the CRC-scanning raw read backend.
    pyrometer_rts=False,
    pyrometer_modbus_backend="raw_serial",
    single_images_folder=r"C:\Dropbox\Data\RHEED\RHEED_YangGroup\FeSeTe_STO",
    stream_images_folder=r"C:\Dropbox\Data\RHEED\RHEED_YangGroup\FeSeTe_STO",
    # ADS: 7 cells on Ch-MBE (Task #191 validated Jul 22 2026).
    # ads_display_confirmed=True because cell_display uses numeric
    # labels aligned with ADS Cell{N} (Cell1=Substrate, Cell2-7 by
    # number). Existing widget-populate behavior preserved.
    ads_netid="10.0.42.112.1.1",
    ads_cell_count=7,
    ads_display_confirmed=True,
)

# Available system configurations. "oxide" and "chalcogenide" are legacy
# aliases preserved for any callers that used the old SYSTEMS dict keys.
SYSTEMS: dict = {
    "ombe":          OXIDE_MBE,
    "oxide":         OXIDE_MBE,
    "chmbe":         CHALCOGENIDE_MBE,
    "chalcogenide":  CHALCOGENIDE_MBE,
}


def get_active_config() -> MBESystemConfig:
    """Return the chamber config selected by the ``AIQM_CHAMBER`` env var.

    Defaults to ``OXIDE_MBE`` (O-MBE / Bulbasaur) when the env var is
    absent or unrecognised. Recognised values (case-insensitive):
    ``ombe``, ``oxide``, ``chmbe``, ``chalcogenide``.
    """
    chamber = os.environ.get("AIQM_CHAMBER", "ombe").lower()
    return SYSTEMS.get(chamber, OXIDE_MBE)
