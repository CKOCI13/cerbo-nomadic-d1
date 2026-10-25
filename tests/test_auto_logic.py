"""Tests for Automatic with limits (automatic = limited, the default).

In Automatic the Cerbo chooses Off, Buck or Boost itself, by the D1 manual's own rules, and sends
the configured current limits with it. These tests check:

  * the decisions, against the manual's thresholds (AutoLogic, with a pretend clock);
  * what the Controller then sends (direction, set-point, current limit);
  * a closed loop against the pretend D1 with an engine that starts and stops, comparing the
    Cerbo's Automatic with the D1's own logic (automatic = hands_off).
"""

import os
import random
import tempfile
import unittest
from unittest import mock

from support import driver, nc, load_config

OFF, BUCK, BOOST = nc.DIRECTION_OFF, nc.DIRECTION_BUCK, nc.DIRECTION_BOOST


def readings(lv, hv=51.0):
    return {'lv_voltage': lv, 'hv_voltage': hv, 'lv_current': 0, 'hv_current': 0, 'temperature': 30}


class AutoLogicTest(unittest.TestCase):
    def setUp(self):
        self.a = driver.AutoLogic()
        self.t = 1000.0

    def hold(self, lv, hv=51.0, seconds=0.0, dt=0.1):
        """Feed the same readings for `seconds` (at least once) and return the last decision."""
        end = self.t + seconds
        d = self.a.decide(readings(lv, hv), self.t)
        while self.t < end:
            self.t += dt
            d = self.a.decide(readings(lv, hv), self.t)
        return d

    def test_standby_between_12_5_and_13_5_volts(self):
        for lv in (12.5, 12.9, 13.4):
            self.assertEqual(self.hold(lv, seconds=10), OFF)
        self.assertIn('Standby', self.a.reason)

    def test_buck_below_12_5_volts_after_two_seconds(self):
        self.assertEqual(self.hold(12.4, seconds=1.5), OFF)
        self.assertEqual(self.hold(12.4, seconds=1.0), BUCK)
        self.assertIn('below 12.5', self.a.reason)

    def test_an_engine_crank_dip_does_not_start_buck(self):
        self.hold(12.8, seconds=5)
        self.assertEqual(self.hold(10.5, seconds=1.2), OFF)   # cranking
        self.assertEqual(self.hold(12.8, seconds=10), OFF)

    def test_no_buck_outside_the_48_volt_window(self):
        self.assertEqual(self.hold(12.0, hv=40.9, seconds=10), OFF)
        self.assertIn('outside 41-58', self.a.reason)
        self.assertEqual(self.hold(12.0, hv=58.1, seconds=10), OFF)
        self.assertEqual(self.hold(12.0, hv=41.0, seconds=3), BUCK)

    def test_buck_stops_at_once_if_the_48_volt_side_leaves_its_window(self):
        self.hold(12.0, seconds=3)
        self.assertEqual(self.hold(12.0, hv=40.5), OFF)

    def test_buck_stops_above_13_5_volts_then_boost_can_start(self):
        self.hold(12.0, seconds=3)
        self.assertEqual(self.hold(13.5, seconds=10), BUCK)   # not above 13.5 yet
        self.assertEqual(self.hold(13.6, seconds=1.5), BUCK)
        self.assertEqual(self.hold(13.6, seconds=1.0), OFF)
        self.assertEqual(self.hold(14.2, seconds=2.5), BOOST)

    def test_boost_from_13_5_to_16_volts(self):
        self.assertEqual(self.hold(13.5, seconds=2.5), BOOST)
        self.setUp()
        self.assertEqual(self.hold(16.1, seconds=10), OFF)    # too high: never convert

    def test_boost_continues_down_to_12_3_volts(self):
        self.hold(14.0, seconds=3)
        self.assertEqual(self.hold(12.4, seconds=10), BOOST)
        self.assertEqual(self.hold(12.2, seconds=2.1), OFF)
        self.assertIn('boost paused', self.a.reason)

    def test_boost_waits_a_minute_after_the_12_volt_side_sagged(self):
        self.hold(14.0, seconds=3)
        self.hold(12.2, seconds=2.5)                            # boost stopped
        self.assertEqual(self.hold(14.0, seconds=50), OFF)
        self.assertIn('boost paused', self.a.reason)
        self.assertEqual(self.hold(14.0, seconds=15), BOOST)

    def test_buck_is_allowed_straight_after_a_boost_stop(self):
        self.hold(14.0, seconds=3)
        self.hold(12.2, seconds=2.5)
        self.assertEqual(self.hold(12.2, seconds=2.5), BUCK)

    def test_boost_stops_at_once_above_16_volts(self):
        self.hold(14.0, seconds=3)
        self.assertEqual(self.hold(16.2), OFF)

    def test_no_readings_means_off(self):
        self.hold(12.0, seconds=3)
        self.assertEqual(self.a.decide(None, self.t), OFF)
        self.assertIn('no readings', self.a.reason)

    def test_never_both_and_never_straight_from_one_to_the_other(self):
        rng = random.Random(4)
        prev = OFF
        for _ in range(20000):
            self.t += rng.choice((0.1, 0.5, 2.0))
            lv = rng.choice((10.5, 12.0, 12.2, 12.4, 12.9, 13.6, 14.2, 16.5))
            hv = rng.choice((40.0, 46.0, 51.0, 57.0, 59.0))
            d = self.a.decide(None if rng.random() < 0.01 else readings(lv, hv), self.t)
            self.assertIn(d, (OFF, BUCK, BOOST))
            self.assertFalse({prev, d} == {BUCK, BOOST}, 'went straight between buck and boost')
            prev = d


class ControllerAutomaticTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.state = os.path.join(tempfile.mkdtemp(), 'state.json')
        self.c = driver.Controller(self.cfg, self.state)
        self.now = 5000.0
        self.lv = 12.9
        test = self

        class FakeTelemetry:
            def fresh(self, can_id):
                return readings(test.lv) if can_id == nc.ID_ELECTRIC else None
        self.c.telemetry = FakeTelemetry()
        patcher = mock.patch.object(driver.time, 'monotonic', lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_for(self, lv, seconds):
        self.lv = lv
        end = self.now + seconds
        f = self.c.frame()
        while self.now < end:
            self.now += 0.01
            f = self.c.frame()
        return nc.decode_ctrl(f)

    def test_buck_is_sent_with_the_buck_limit_and_the_manuals_13_4_volt_target(self):
        f = self.run_for(12.2, 3)
        self.assertEqual(f['direction'], BUCK)
        self.assertEqual(f['current_limit'], 100)
        self.assertAlmostEqual(f['voltage_setpoint'], 13.4, delta=0.01)   # buck_voltage 13.5, capped

    def test_boost_is_sent_with_the_boost_limit(self):
        f = self.run_for(14.2, 3)
        self.assertEqual(f['direction'], BOOST)
        self.assertEqual(f['current_limit'], 20)
        self.assertAlmostEqual(f['voltage_setpoint'], 55.2, delta=0.01)

    def test_standby_sends_off(self):
        self.assertEqual(self.run_for(12.9, 10)['direction'], OFF)

    def test_owner_limits_are_used_and_capped_by_config(self):
        self.c.update({'buck_current_limit': 60})
        self.assertEqual(self.run_for(12.2, 3)['current_limit'], 60)
        with self.assertRaises(ValueError):
            self.c.update({'buck_current_limit': 121})   # config maximum for the LTV1 is 120

    def test_reversing_still_passes_through_off(self):
        self.run_for(12.2, 3)                                # buck
        sent = []
        self.lv = 14.2
        for _ in range(1000):                                # 10 s
            self.now += 0.01
            sent.append(nc.decode_ctrl(self.c.frame())['direction'])
        self.assertIn(BOOST, sent)
        first_boost = sent.index(BOOST)
        self.assertEqual(set(sent[:first_boost]) - {BUCK, OFF}, set())
        last_buck = max(i for i, d in enumerate(sent) if d == BUCK)
        self.assertGreaterEqual((first_boost - last_buck) * 0.01, 1.0)   # at least 1 s of Off

    def test_back_to_automatic_starts_again_from_standby(self):
        self.run_for(14.2, 3)                                # Automatic chose boost
        self.c.update({'direction': BUCK})
        self.run_for(12.4, 5)                                # owner's buck for a while
        self.c.update({'direction': driver.DIRECTION_AUTO})
        # 12.4 V: an old boost would carry on down to 12.3 V; afresh, it waits 2 s and then bucks
        self.assertEqual(self.run_for(12.4, 1)['direction'], OFF)
        self.assertEqual(self.run_for(12.4, 2)['direction'], BUCK)

    def test_choosing_buck_by_hand_still_works(self):
        self.run_for(14.2, 3)
        self.c.update({'direction': BUCK})
        self.assertEqual(self.run_for(14.2, 2)['direction'], BUCK)   # owner's choice, not Automatic
        self.assertIsNone(self.c.snapshot()['auto_reason'])


def closed_loop(automatic, seconds=1200.0, dt=0.05):
    """Run the Controller against the pretend D1 (engine off/on every 3 minutes) on a pretend
    clock. Returns (seconds, 12 V-side volts, 12 V-side amps, sent direction) samples."""
    cfg = load_config()
    cfg['control']['automatic'] = automatic
    c = driver.Controller(cfg, os.path.join(tempfile.mkdtemp(), 's.json'))
    sim = driver.SimulatedD1(engine=True)
    clock = [sim._start]

    class Telemetry:
        def fresh(self, can_id):
            return nc.decode_electric(sim._frame(nc.ID_ELECTRIC, clock[0]))
    c.telemetry = Telemetry()
    samples = []
    with mock.patch.object(driver.time, 'monotonic', lambda: clock[0]):
        while clock[0] < sim._start + seconds:
            clock[0] += dt
            frame = c.frame()
            if frame is not None:
                sim.ctrl, sim.ctrl_time = nc.decode_ctrl(frame), clock[0]
            sim.step(clock[0])
            samples.append((clock[0] - sim._start, sim.lv, sim.lv_i, c.snapshot()['sent_direction']))
    return samples


class ClosedLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.limited = closed_loop('limited')
        cls.hands_off = closed_loop('hands_off')

    def test_the_cerbo_keeps_the_12_volt_cable_within_its_limit(self):
        # Buck limit 100 A; boost limit 20 A on the 48 V side is about 80-100 A on the 12 V side.
        self.assertLessEqual(max(abs(s[2]) for s in self.limited), 120)

    def test_left_alone_the_d1_draws_far_more_than_a_150_amp_fuse(self):
        # Why the LTV1 needs this: the D1's own logic at its factory limits.
        self.assertGreater(max(abs(s[2]) for s in self.hands_off), 150)

    def test_it_bucks_and_boosts_by_itself(self):
        sent = {s[3] for s in self.limited}
        self.assertEqual(sent, {OFF, BUCK, BOOST})
        self.assertTrue(any(s[2] > 50 for s in self.limited))     # bucking: into the 12 V side
        self.assertTrue(any(s[2] < -50 for s in self.limited))    # boosting: out of it

    def test_it_does_not_flap_between_buck_and_boost(self):
        changes = sum(1 for a, b in zip(self.limited, self.limited[1:]) if a[3] != b[3])
        # 20 minutes of engine off / on: a handful of changes, not one every few seconds
        self.assertLess(changes, 30)

    def test_the_12_volt_side_never_drops_far(self):
        self.assertGreater(min(s[1] for s in self.limited), 12.0)


if __name__ == '__main__':
    unittest.main()
