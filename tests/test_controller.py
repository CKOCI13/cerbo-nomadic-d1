"""Tests for the Controller: what the driver tells the D1, and the safety rules around it.

The Controller holds the commanded state (Automatic / Off / Buck / Boost, set-points and limits)
and builds the EMS_DCDC_CTRL frame that is sent every 10 ms.
"""

import json
import os
import tempfile
import time
import unittest

from support import driver, nc, load_config


def near(a, b):
    """Voltages are sent in 15 mV steps, so a set-point comes back within half a step."""
    return abs(a - b) < 0.0075


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg['control']['direction_change_delay'] = '0.3'   # short, so the tests run fast
        self.state = os.path.join(tempfile.mkdtemp(), 'state.json')
        self.c = driver.Controller(self.cfg, self.state)

    def test_starts_in_automatic_and_sends_nothing(self):
        self.assertEqual(self.c.snapshot()['direction'], driver.DIRECTION_AUTO)
        self.assertIsNone(self.c.frame())
        self.assertIsNone(self.c.snapshot()['sent_direction'])

    def test_buck_and_boost_use_their_own_set_point_and_current_limit(self):
        self.c.update({'direction': nc.DIRECTION_BUCK})
        f = nc.decode_ctrl(self.c.frame())
        self.assertEqual(f['direction'], 1)
        self.assertTrue(near(f['voltage_setpoint'], 13.5))
        self.assertEqual(f['current_limit'], 100)

        self.c.update({'direction': nc.DIRECTION_BOOST})
        time.sleep(0.35)   # past the reversal pause
        f = nc.decode_ctrl(self.c.frame())
        self.assertEqual(f['direction'], 2)
        self.assertTrue(near(f['voltage_setpoint'], 55.2))
        self.assertEqual(f['current_limit'], 30)

    def test_reversing_always_passes_through_off(self):
        self.c.update({'direction': 1})
        self.c.frame()
        self.c.update({'direction': 2})
        self.assertEqual(nc.decode_ctrl(self.c.frame())['direction'], 0)   # held off first
        time.sleep(0.35)
        self.assertEqual(nc.decode_ctrl(self.c.frame())['direction'], 2)
        # Boost -> Automatic -> Buck in quick succession is still held off
        self.c.update({'direction': driver.DIRECTION_AUTO})
        self.assertIsNone(self.c.frame())
        self.c.update({'direction': 1})
        self.assertEqual(nc.decode_ctrl(self.c.frame())['direction'], 0)
        time.sleep(0.35)
        self.assertEqual(nc.decode_ctrl(self.c.frame())['direction'], 1)

    def test_off_remembers_the_last_choice(self):
        self.c.update({'direction': 1})
        self.c.update({'direction': 0})
        f = nc.decode_ctrl(self.c.frame())
        self.assertEqual(f['direction'], 0)
        self.assertEqual(self.c.snapshot()['last_direction'], 1)

    def test_refuses_values_outside_the_limits(self):
        bad_changes = [
            {'buck_voltage': 13.6}, {'buck_voltage': 9.9}, {'boost_voltage': 57.9},
            {'boost_voltage': 41.9}, {'buck_current_limit': 179}, {'boost_current_limit': 58},
            {'boost_current_limit': 10.5}, {'precharge_voltage': 60}, {'direction': 3},
            {'direction': -2}, {'direction': '1'}, {'precharge': 2}, {'buck_voltage': 'abc'},
            {'buck_voltage': float('nan')}, {'current_limit': 10}, {},
            {'buck_voltage': 13.0, 'boost_current_limit': 999},   # one bad value rejects all
        ]
        for bad in bad_changes:
            with self.subTest(change=bad):
                with self.assertRaises(ValueError):
                    self.c.update(bad)
        self.assertEqual(self.c.snapshot()['buck_voltage'], 13.5)   # nothing changed

    def test_settings_are_saved_but_startup_is_automatic(self):
        self.c.update({'buck_voltage': 13.2, 'buck_current_limit': 178, 'direction': 0,
                       'precharge': True})
        saved = json.load(open(self.state))
        self.assertEqual(saved['buck_current_limit'], 178)
        restarted = driver.Controller(self.cfg, self.state).snapshot()
        self.assertEqual(restarted['direction'], driver.DIRECTION_AUTO)
        self.assertEqual(restarted['buck_voltage'], 13.2)
        self.assertIs(restarted['precharge'], False)   # never restored

    def test_startup_choices(self):
        self.c.update({'direction': 0})
        for choice, expected in (('last', 0), ('off', 0), ('auto', -1)):
            self.cfg['control']['startup'] = choice
            self.assertEqual(driver.Controller(self.cfg, self.state).snapshot()['direction'],
                             expected)
        self.cfg['control']['startup'] = 'bogus'
        with self.assertRaises(ValueError):
            driver.Controller(self.cfg, self.state)

    def test_test_timer_switches_buck_to_off(self):
        self.cfg['control']['test_timer'] = '0.01'   # 0.6 s
        c = driver.Controller(self.cfg, os.path.join(tempfile.mkdtemp(), 's.json'))
        c.update({'direction': 1})
        self.assertIsNotNone(c.snapshot()['test_timer_left'])
        time.sleep(0.7)
        self.assertEqual(nc.decode_ctrl(c.frame())['direction'], 0)
        self.assertEqual(c.snapshot()['direction'], 0)
        self.assertIsNone(c.snapshot()['test_timer_left'])


if __name__ == '__main__':
    unittest.main()
