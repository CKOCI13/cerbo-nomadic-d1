"""Tests for the pretend D1 used by ``--simulate`` (Cerbo Setup option 9).

The pretend D1 lets people try every screen and control with no converter connected. These
tests check that it behaves like the D1 manual describes, and that the whole driver works
against it end to end.
"""

import os
import tempfile
import time
import unittest

from support import driver, nc, load_config


def run(sim, t, seconds, ctrl=None, dt=0.05):
    """Advance the model by `seconds` of pretend time, optionally sending a control frame."""
    end = t + seconds
    while t < end:
        t += dt
        if ctrl is not None:
            sim.ctrl, sim.ctrl_time = nc.decode_ctrl(ctrl), t
        sim.step(t)
    return t


class PretendD1Test(unittest.TestCase):
    def setUp(self):
        self.sim = driver.SimulatedD1(engine=False)   # loads only: the 12 V side keeps draining
        self.t = self.sim._last_step

    def test_automatic_logic_follows_the_manual(self):
        sim = self.sim
        t = run(sim, self.t, 40)
        self.assertEqual(sim.mode, 0x2)            # standby while the 12 V side drains
        self.assertTrue(12.5 < sim.lv < 12.9)
        t = run(sim, t, 30)
        self.assertEqual(sim.mode, 0x5)            # below 12.5 V: autonomous buck
        self.assertGreater(sim.lv_i, 0)
        t = run(sim, t, 120)
        self.assertAlmostEqual(sim.lv, 13.4, delta=0.05)

    def test_obeys_buck_off_and_boost(self):
        sim = self.sim
        t = run(sim, self.t, 120, ctrl=nc.encode_ctrl(1, False, 30, 13.5, 48))
        self.assertEqual(sim.mode, 0x4)
        self.assertAlmostEqual(sim.lv, 13.5, delta=0.05)
        self.assertLessEqual(sim.lv_i, 30)
        t = run(sim, t, 2, ctrl=nc.encode_ctrl(0, False, 30, 13.5, 48))
        self.assertEqual((sim.mode, sim.lv_i), (0x2, 0))
        hv_before = sim.hv
        boost = nc.encode_ctrl(2, False, 5, 52.0, 48)
        t = run(sim, t, 20, ctrl=boost)
        self.assertEqual(sim.mode, 0x3)
        self.assertGreater(sim.hv, hv_before)
        self.assertEqual(sim.hv_i, 5)
        self.assertLess(sim.lv_i, 0)               # the 12 V side is the source
        for _ in range(20000):                     # boost until it protects the 12 V side
            t = run(sim, t, 0.05, ctrl=boost)
            if sim.mode != 0x3:
                break
        self.assertEqual(sim.mode, 0x2)
        self.assertTrue(12.25 <= sim.lv < 12.3)

    def test_flags_can_loss_when_commands_stop(self):
        sim = self.sim
        t = run(sim, self.t, 2, ctrl=nc.encode_ctrl(1, False, 30, 13.5, 48))
        t = run(sim, t, 1.5)
        alarms = nc.alarm_texts(nc.decode_parameter(sim._frame(nc.ID_PARAMETER, t)))
        self.assertIn('CAN loss', alarms)
        t = run(sim, t, 12)
        alarms = nc.alarm_texts(nc.decode_parameter(sim._frame(nc.ID_PARAMETER, t)))
        self.assertNotIn('CAN loss', alarms)


class EndToEndTest(unittest.TestCase):
    def test_driver_threads_against_the_pretend_d1(self):
        cfg = load_config()
        driver.simulation_config(cfg)
        self.assertEqual(cfg['dbus']['custom_name'], 'DC-DC D1 (SIMULATED)')
        self.assertEqual(cfg['can']['interface'], 'sim')
        cfg['control']['test_timer'] = '0.02'      # 1.2 s
        c = driver.Controller(cfg, os.path.join(tempfile.mkdtemp(), 's.json'))
        telemetry, stats, bus = driver.Telemetry(1.0), driver.TxStats(), driver.SimulatedD1()
        c.telemetry = telemetry
        driver.start_thread('rx', driver.rx_worker, bus, telemetry)
        driver.start_thread('tx', driver.tx_worker, bus, c, stats)
        time.sleep(0.6)
        s = telemetry.snapshot()
        self.assertTrue(s['online'])
        self.assertGreater(stats.sent, 0)          # Automatic with limits: the Cerbo is in charge
        self.assertEqual(c.snapshot()['sent_direction'], 0)   # 12.9 V: standby
        c.update({'direction': 1})
        time.sleep(0.5)
        self.assertEqual(telemetry.snapshot()['parameter']['operating_mode'], 0x4)
        self.assertGreater(stats.sent, 30)
        time.sleep(1.2)
        self.assertEqual(c.snapshot()['direction'], 0)   # the test timer switched it off


if __name__ == '__main__':
    unittest.main()
