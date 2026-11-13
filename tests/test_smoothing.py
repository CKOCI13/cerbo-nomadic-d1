"""Averaging of voltage, current and power before the Cerbo shows them ([dbus] smoothing).

The converter reports current in whole amps, so a steady 36.4 A flips between 36 and 37 A and the
readings jump by about 55 W at 54 V (seen in a field test on 2026-11-13). The averages are for the
screens only: the control logic keeps using the latest reading.
"""

import os
import tempfile
import time
import unittest

from support import driver, nc, load_config


def electric(hv_current, hv_voltage=54.3, lv_voltage=13.9, lv_current=0, temperature=40):
    return nc.encode_electric(lv_voltage, lv_current, hv_voltage, hv_current, temperature)


LATEST = {'lv_voltage': 13.9, 'lv_current': 0, 'hv_voltage': 54.3, 'hv_current': 36, 'temperature': 40}


class SmoothingTest(unittest.TestCase):
    def test_shipped_config_and_default(self):
        self.assertEqual(load_config()['dbus'].getfloat('smoothing'), 5.0)
        self.assertEqual(driver.DEFAULTS['dbus']['smoothing'], '5')   # an older config.ini without it

    def test_whole_amp_flicker_is_averaged(self):
        t = driver.Telemetry(60, 5)
        for i in range(500):   # 36 / 37 A in a 60/40 mix = 36.4 A, 100 frames a second
            t.feed(nc.ID_ELECTRIC, electric(37 if i % 5 in (1, 3) else 36), now=1000 + i / 100)
        a = t.smoothed(LATEST, now=1004.99)
        self.assertAlmostEqual(a['hv_current'], 36.4, places=2)
        self.assertAlmostEqual(a['hv_power'], 36.4 * 54.3, delta=1)

    def test_old_readings_leave_the_window(self):
        t = driver.Telemetry(60, 5)
        for i in range(500):
            t.feed(nc.ID_ELECTRIC, electric(36), now=100 + i / 100)
        for i in range(500):
            t.feed(nc.ID_ELECTRIC, electric(10), now=105 + i / 100)
        self.assertAlmostEqual(t.smoothed(LATEST, now=110.0)['hv_current'], 10, places=6)

    def test_off_and_offline(self):
        t = driver.Telemetry(60, 0)
        t.feed(nc.ID_ELECTRIC, electric(37))
        self.assertEqual(t.snapshot()['smoothed']['hv_current'], 37)
        self.assertIsNone(driver.Telemetry(1, 5).snapshot()['smoothed'])

    def test_control_uses_the_latest_reading(self):
        t = driver.Telemetry(60, 5)
        now = time.monotonic()
        t.feed(nc.ID_ELECTRIC, electric(10, temperature=40), now=now - 1)
        t.feed(nc.ID_ELECTRIC, electric(36, temperature=50), now=now)
        self.assertEqual(t.fresh(nc.ID_ELECTRIC)['hv_current'], 36)   # what the controller reads
        snap = t.snapshot()
        self.assertEqual(snap['electric']['hv_current'], 36)
        self.assertEqual(snap['smoothed']['hv_current'], 23)
        self.assertEqual(snap['smoothed']['temperature'], 50)


class SmoothedOnTheCerboTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        cfg = load_config()
        self.c = driver.Controller(cfg, os.path.join(tmp, 'state.json'))
        self.d = driver.DbusService(cfg, self.c, driver.UiSettings(os.path.join(tmp, 'ui.json')))

    def powers(self, smoothing):
        t = driver.Telemetry(60, smoothing)
        start = time.monotonic() - 4.98
        seen = []
        for i in range(499):
            t.feed(nc.ID_ELECTRIC, electric(37 if i % 5 in (1, 3) else 36), now=start + i / 100)
            if i >= 400 and i % 7 == 0:   # not a multiple of 5, so it catches both values
                self.d.refresh(t.snapshot(), self.c.snapshot())
                seen.append(self.d.svc['/Dc/In/P'])
        return seen

    def test_without_averaging_the_reading_jumps(self):
        seen = self.powers(0)
        self.assertGreaterEqual(max(seen) - min(seen), 50)

    def test_with_averaging_the_reading_is_steady(self):
        seen = self.powers(5)
        self.assertLessEqual(max(seen) - min(seen), 3)
        self.assertAlmostEqual(self.d.svc['/Dc/In/I'], 36.4, delta=0.05)
        self.assertEqual(self.d.svc['/GenericInput/hv_current/Settings/Decimals'], 1)


if __name__ == '__main__':
    unittest.main()
