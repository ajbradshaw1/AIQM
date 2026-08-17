#!/usr/bin/env python3
"""Tests for the pyrometer RTS control-line setting.

Root cause of every silent/echo pyrometer verdict recorded Jul 28 and
Jul 30 2026. pyserial asserts RTS by default; on Bulbasaur's IFD-5 (RS232
position) + Prolific PL2303GS path an asserted RTS loops the link back, so
every request returns byte-for-byte and the probe is never reached.

Measured on COM4, Jul 30 2026, request 01 03 13 00 00 01 80 8E:

    RTS=True,  DTR=True   -> 01 03 13 00 00 01 80 8E   (loopback)
    RTS=True,  DTR=False  -> 01 03 13 00 00 01 80 8E   (loopback)
    RTS=False, DTR=True   -> 01 03 02 09 03 FE 15      (version 9.3)
    RTS=False, DTR=False  -> 01 03 02 09 03 FE 15      (version 9.3)

RTS alone determines the outcome; DTR is irrelevant and is deliberately
left unconfigured.

No hardware: these assert the configuration surface and that the driver
accepts and stores the setting.
"""
import unittest

from drivers.config import CHALCOGENIDE_MBE, OXIDE_MBE, MBESystemConfig
from drivers.pyrometer import ModbusPyrometer


class ConfigSurfaceTests(unittest.TestCase):
    def test_default_is_none_not_false(self):
        """Unconfigured must mean "no claim", never a silent behaviour change.

        Defaulting to False would export an O-MBE measurement to every
        chamber that has not been tested, changing their serial behaviour
        without evidence. None leaves them exactly as they were.
        """
        self.assertIsNone(MBESystemConfig.pyrometer_rts)

    def test_ombe_is_false(self):
        """O-MBE is the verified case — probe answered with RTS de-asserted."""
        self.assertFalse(OXIDE_MBE.pyrometer_rts)
        self.assertIsNotNone(OXIDE_MBE.pyrometer_rts)

    def test_chmbe_verified_false(self):
        """Ch-MBE measured 2026-08-05 — no longer an unset chamber.

        This test previously asserted None, guarding against a well-meaning
        edit copying O-MBE's False across "for consistency". Its docstring
        required measuring the probe before changing it. That measurement
        happened: pyrometer_raw_modbus_probe.py on COM3 (Prolific PL2303GS,
        115200, id 1) with RTS de-asserted returned

            REG_VER  RX 01 03 02 09 03 FE 15         -> version 9.3
            REG_CH1  RX 01 03 04 43 55 D7 2F E1 8B   -> 213.84 C

        byte-identical to Bulbasaur's validated REG_VER reply, verdict
        device_replied / non_echo_response. The value is now a measurement,
        not an inherited assumption.

        The "don't copy across unmeasured" invariant still holds for any
        future chamber — see test_unset_chamber_propagates_as_none.
        """
        self.assertIs(CHALCOGENIDE_MBE.pyrometer_rts, False)
        self.assertIsNotNone(CHALCOGENIDE_MBE.pyrometer_rts)

    def test_chmbe_port_is_com3(self):
        """Ch-MBE's probe is on COM3, not the COM4 dataclass default.

        Verified 2026-08-05: pyserial enumerated only COM1 (motherboard)
        and COM3 (Prolific PL2303GS USB Serial). The GUI polled COM4 and
        logged "No response received after 3 retries" until this was set.
        """
        self.assertEqual(CHALCOGENIDE_MBE.pyrometer_port, "COM3")

    def test_chmbe_uses_verified_raw_false_configuration(self):
        """Ch-MBE COM3 answered raw Modbus with RTS de-asserted."""
        self.assertEqual(CHALCOGENIDE_MBE.pyrometer_port, "COM3")
        self.assertEqual(CHALCOGENIDE_MBE.pyrometer_baudrate, 115200)
        self.assertIs(CHALCOGENIDE_MBE.pyrometer_rts, False)
        self.assertEqual(CHALCOGENIDE_MBE.pyrometer_modbus_backend, "raw_serial")

    def test_setting_is_per_chamber_not_module_global(self):
        """A future edit must not collapse this into one shared constant."""
        self.assertIn("pyrometer_rts", MBESystemConfig.__dataclass_fields__)


class DriverAcceptsRtsTests(unittest.TestCase):
    def test_defaults_to_none(self):
        """Constructing the driver must not change anyone's serial state."""
        self.assertIsNone(ModbusPyrometer()._rts)

    def test_stores_explicit_value(self):
        self.assertTrue(ModbusPyrometer(rts=True)._rts)
        self.assertFalse(ModbusPyrometer(rts=False)._rts)

    def test_false_is_distinguishable_from_unset(self):
        """`if self._rts:` would treat False and None alike — it must not.

        False means "de-assert the line"; None means "do not touch it".
        Collapsing them reintroduces exactly the untested rollout this
        design exists to prevent.
        """
        self.assertIsNotNone(ModbusPyrometer(rts=False)._rts)
        self.assertIsNone(ModbusPyrometer()._rts)

    def test_constructing_opens_no_port(self):
        """Construction must stay inert — connect() owns all I/O."""
        p = ModbusPyrometer(port="COM_DOES_NOT_EXIST", rts=False)
        self.assertFalse(p.connected)


class WorkerWiringTests(unittest.TestCase):
    """The GUI must thread pyrometer_rts from chamber config to the driver.

    On Jul 30 2026 the standalone scripts read the probe successfully and
    then the GUI, launched seconds later on the same port, failed — because
    PyrometerWorker._create_sensor called ModbusPyrometer() with no
    arguments at all, so the driver got its own None default instead of
    OXIDE_MBE.pyrometer_rts. The console said exactly that, which is the
    only reason it took seconds rather than another afternoon.
    """

    def test_worker_accepts_and_stores_rts(self):
        from gui.workers import PyrometerWorker

        self.assertIsNone(PyrometerWorker(mode="modbus").rts)
        self.assertFalse(PyrometerWorker(mode="modbus", rts=False).rts)
        self.assertIsNotNone(PyrometerWorker(mode="modbus", rts=False).rts)

    def test_forwards_all_three_inputs_to_the_driver(self):
        """port, baudrate and rts must all reach ModbusPyrometer.

        The factory previously called ModbusPyrometer() bare, so every one
        of these was silently replaced by a driver default.
        """
        from gui.workers import PyrometerWorker

        sensor = PyrometerWorker(
            mode="modbus", port="COM9", baudrate=57600, rts=False,
        )._create_sensor()
        self.assertEqual(sensor._port, "COM9")
        self.assertEqual(sensor._baudrate, 57600)
        self.assertFalse(sensor._rts)

    def test_ombe_config_reaches_the_driver_as_false(self):
        """The exact path that failed in the Jul 30 GUI session."""
        from gui.workers import PyrometerWorker

        sensor = PyrometerWorker(
            mode="modbus", rts=OXIDE_MBE.pyrometer_rts,
        )._create_sensor()
        self.assertIs(sensor._rts, False)

    def test_unset_chamber_propagates_as_none(self):
        """An uncharacterised chamber must reach the driver as None.

        Ch-MBE was the original example here; it is now measured (False as
        of 2026-08-05), so this uses a bare MBESystemConfig instead. The
        invariant is what matters and it is not chamber-specific: any
        chamber whose cabling nobody has measured must make no claim, so
        the GUI does not de-assert RTS on untested hardware.
        """
        from gui.workers import PyrometerWorker

        unmeasured = MBESystemConfig(name="Unmeasured chamber")
        self.assertIsNone(unmeasured.pyrometer_rts)

        sensor = PyrometerWorker(
            mode="modbus", rts=unmeasured.pyrometer_rts,
        )._create_sensor()
        self.assertIsNone(sensor._rts)

    def test_chmbe_config_reaches_the_driver_as_false(self):
        """Ch-MBE's measured value must survive the trip to the driver."""
        from gui.workers import PyrometerWorker

        sensor = PyrometerWorker(
            mode="modbus", rts=CHALCOGENIDE_MBE.pyrometer_rts,
            device_id=CHALCOGENIDE_MBE.pyrometer_device_id,
            modbus_backend=CHALCOGENIDE_MBE.pyrometer_modbus_backend,
        )._create_sensor()
        self.assertIs(sensor._rts, False)
        self.assertEqual(sensor._device_id, CHALCOGENIDE_MBE.pyrometer_device_id)
        self.assertEqual(sensor._backend, "raw_serial")
