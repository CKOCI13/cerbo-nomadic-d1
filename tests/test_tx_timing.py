"""Tests for the control-command timing figures (TxStats).

The D1 expects the Cerbo's control message (0x018) every 10 ms. These figures show how evenly
the Cerbo actually sends it, so a slow or busy Cerbo shows up as late commands instead of going
unnoticed, and a Cerbo GX MK2 can be compared with an MK3.
"""

import os
import tempfile
import time
import unittest

from support import driver, nc, load_config, ELECTRIC_FRAME, PARAMETER_FRAME


def steady(stats, start, count, period=0.010):
    t = start
    for _ in range(count):
        stats.sent_frame(t)
        t += period
    return t


class TxStatsTest(unittest.TestCase):
    def test_nothing_sent_yet(self):
        t = driver.TxStats().timing(now=100.0)
        self.assertIsNone(t['rate'])
        self.assertIsNone(t['worst_gap_ms'])
        self.assertIsNone(t['worst_gap_ever_ms'])
        self.assertEqual((t['late'], t['long'], t['late_last_minute']), (0, 0, 0))

    def test_steady_10_ms_is_100_per_second_and_never_late(self):
        s = driver.TxStats()
        end = steady(s, 1000.0, 1000)            # 10 s at 10 ms
        t = s.timing(now=end)
        self.assertAlmostEqual(t['rate'], 100, delta=1)
        self.assertEqual(t['worst_gap_ms'], 10)
        self.assertEqual((t['late'], t['long']), (0, 0))
        self.assertEqual(s.sent, 1000)

    def test_a_late_and_a_long_gap_are_counted(self):
        s = driver.TxStats()
        t = steady(s, 1000.0, 50)
        s.sent_frame(t + 0.025)                  # 35 ms after the last: late, not long
        t = steady(s, t + 0.025 + 0.010, 50)
        s.sent_frame(t + 0.140)                  # 150 ms: late and long
        timing = s.timing(now=t + 0.2)
        self.assertEqual(timing['late'], 2)
        self.assertEqual(timing['late_last_minute'], 2)
        self.assertEqual(timing['long'], 1)
        self.assertEqual(timing['worst_gap_ms'], 150)
        self.assertEqual(timing['worst_gap_ever_ms'], 150)

    def test_a_pause_is_not_a_gap(self):
        s = driver.TxStats()
        t = steady(s, 1000.0, 10)
        s.paused()                               # e.g. hands-off automatic for 5 s
        steady(s, t + 5.0, 10)
        timing = s.timing(now=t + 5.2)
        self.assertEqual(timing['late'], 0)
        self.assertEqual(timing['worst_gap_ms'], 10)

    def test_last_minute_figures_forget_old_gaps(self):
        s = driver.TxStats()
        t = steady(s, 1000.0, 10)
        s.sent_frame(t + 0.5)                    # one 510 ms gap
        t = steady(s, t + 0.51, 7000)            # then 70 s of steady sending
        timing = s.timing(now=t)
        self.assertEqual(timing['worst_gap_ms'], 10)
        self.assertEqual(timing['late_last_minute'], 0)
        self.assertEqual(timing['late'], 1)      # since start: still remembered
        self.assertEqual(timing['worst_gap_ever_ms'], 510)


class TxWorkerTimingTest(unittest.TestCase):
    def test_the_tx_thread_feeds_the_figures(self):
        cfg = load_config()
        driver.simulation_config(cfg)
        c = driver.Controller(cfg, os.path.join(tempfile.mkdtemp(), 's.json'))
        telemetry, stats, bus = driver.Telemetry(1.0), driver.TxStats(), driver.SimulatedD1()
        c.telemetry = telemetry
        driver.start_thread('rx', driver.rx_worker, bus, telemetry)
        driver.start_thread('tx', driver.tx_worker, bus, c, stats)
        time.sleep(2.2)
        t = stats.timing()
        self.assertGreater(stats.sent, 50)
        self.assertIsNotNone(t['rate'])
        self.assertGreater(t['rate'], 20)        # loose: test PCs can be busy
        self.assertIsNotNone(t['worst_gap_ms'])


class TimingOnScreensTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        tmp = tempfile.mkdtemp()
        self.c = driver.Controller(self.cfg, os.path.join(tmp, 'state.json'))
        self.t = driver.Telemetry(60)
        self.t.feed(nc.ID_ELECTRIC, ELECTRIC_FRAME)
        self.t.feed(nc.ID_PARAMETER, PARAMETER_FRAME)
        self.stats = driver.TxStats()
        now = time.monotonic()
        steady(self.stats, now - 3.0, 200)   # ends 1 s ago
        self.stats.sent_frame(now - 0.5)         # one 510 ms gap (last steady frame was 1.01 s ago)

    def test_dbus_paths_and_device_card(self):
        d = driver.DbusService(self.cfg, self.c, driver.UiSettings(
            os.path.join(tempfile.mkdtemp(), 'ui.json')))
        d.tx_stats = self.stats
        d.refresh(self.t.snapshot(), self.c.snapshot())
        paths = d.svc.paths
        self.assertEqual(paths['/Nomadic/Tx/WorstGap']['value'], 510)
        self.assertEqual(paths['/Nomadic/Tx/LongGaps']['value'], 1)
        self.assertEqual(paths['/Nomadic/Tx/Sent']['value'], 201)
        self.assertEqual(paths['/GenericInput/command_gap/Value']['value'], 510)
        self.assertEqual(paths['/GenericInput/command_gap/Settings/Unit']['value'], 'ms')
        self.assertEqual(paths['/GenericInput/late_commands/Value']['value'], 1)

    def test_dbus_without_stats_shows_nothing(self):
        d = driver.DbusService(self.cfg, self.c, driver.UiSettings(
            os.path.join(tempfile.mkdtemp(), 'ui.json')))
        d.refresh(self.t.snapshot(), self.c.snapshot())
        self.assertIsNone(d.svc.paths['/Nomadic/Tx/WorstGap']['value'])

    def test_web_status_and_page(self):
        app = driver.WebApp(self.cfg, self.c, self.t, self.stats)
        timing = app.status()['tx']['timing']
        self.assertEqual(timing['worst_gap_ms'], 510)
        self.assertEqual(timing['long'], 1)
        with open(os.path.join(driver.BASE_DIR, 'webui.html')) as f:
            page = f.read()
        for element in ('tx-rate', 'tx-gap', 'tx-late', 'tx-long'):
            self.assertIn('id="%s"' % element, page)


if __name__ == '__main__':
    unittest.main()
