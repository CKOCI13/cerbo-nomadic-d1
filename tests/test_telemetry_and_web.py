"""Tests for the readings (Telemetry) and the built-in web page's API."""

import base64
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from support import driver, nc, load_config, free_port, ELECTRIC_FRAME, PARAMETER_FRAME, VERSION_FRAME


class TelemetryTest(unittest.TestCase):
    def test_readings_go_stale_but_the_version_is_kept(self):
        t = driver.Telemetry(0.2)
        self.assertFalse(t.snapshot()['online'])
        t.feed(nc.ID_ELECTRIC, ELECTRIC_FRAME)
        t.feed(nc.ID_PARAMETER, PARAMETER_FRAME)
        t.feed(nc.ID_VERSION, VERSION_FRAME)
        s = t.snapshot()
        self.assertTrue(s['online'])
        self.assertEqual(s['electric']['lv_voltage'], 13.6)
        time.sleep(0.25)
        s = t.snapshot()
        self.assertFalse(s['online'])
        self.assertIsNone(s['electric'])
        self.assertEqual(s['version']['software'], '1.2.3')

    def test_short_frames_are_ignored(self):
        t = driver.Telemetry(1)
        t.feed(nc.ID_ELECTRIC, bytes(4))
        self.assertEqual(t.bad_frames, 1)
        self.assertFalse(t.snapshot()['online'])


class WebApiTest(unittest.TestCase):
    """Starts the real web server on a free local port and talks to it like the page does."""

    @classmethod
    def setUpClass(cls):
        cfg = load_config()
        cfg['web']['port'] = str(free_port())
        cfg['web']['bind'] = '127.0.0.1'
        cfg['web']['password'] = 'pw'
        cls.controller = driver.Controller(cfg, os.path.join(tempfile.mkdtemp(), 'state.json'))
        telemetry = driver.Telemetry(60)
        telemetry.feed(nc.ID_ELECTRIC, ELECTRIC_FRAME)
        telemetry.feed(nc.ID_PARAMETER, PARAMETER_FRAME)
        stats = driver.TxStats()
        driver.start_web(cfg, driver.WebApp(cfg, cls.controller, telemetry, stats))
        cls.base = 'http://127.0.0.1:%s/' % cfg['web']['port']
        cls.auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:pw').decode()}
        time.sleep(0.2)

    def request(self, path, body=None, headers=None, content_type='application/json'):
        h = dict(headers if headers is not None else self.auth)
        if body is not None:
            h['Content-Type'] = content_type
        r = urllib.request.Request(self.base + path, data=None if body is None else body.encode(),
                                   headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_password_is_required(self):
        self.assertEqual(self.request('', headers={})[0], 401)

    def test_page_and_status(self):
        code, page = self.request('')
        self.assertEqual(code, 200)
        self.assertIn(b'data-dir="-1"', page)   # the Automatic button
        code, body = self.request('api/status')
        st = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(st['online'])
        self.assertEqual(st['temperature_unit'], 'F')   # no D-Bus here: Fahrenheit
        self.assertEqual(st['alarms'][0], 'CAN loss')   # most serious first
        self.assertEqual(st['control']['limits']['boost_current_limit'], [0, 23])

    def test_control_is_validated(self):
        # Other web sites can't post here: a JSON content type is required
        self.assertEqual(self.request('api/control', '{"direction": 2}', content_type='text/plain')[0], 415)
        code, body = self.request('api/control', '{"boost_current_limit": 60}')
        self.assertEqual(code, 400)
        self.assertIn(b'boost_current_limit', body)
        self.assertEqual(self.request('api/control', 'not json')[0], 400)
        code, body = self.request('api/control', '{"boost_voltage": 56.8, "direction": -1}')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['control']['boost_voltage'], 56.8)


if __name__ == '__main__':
    unittest.main()
