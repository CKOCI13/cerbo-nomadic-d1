"""Tests for what appears on the Cerbo screen, in VRM and over MQTT (the D-Bus service).

The checks marked "gui-v2" follow the rules in Victron's GUI source code (iochannel.cpp,
switchableoutput.cpp) that decide whether an item is shown in the switch pane at all.
"""

import os
import tempfile
import unittest

from support import driver, nc, load_config, ELECTRIC_FRAME, PARAMETER_FRAME, VERSION_FRAME


class DbusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = load_config()
        self.c = driver.Controller(self.cfg, os.path.join(self.tmp, 'state.json'))
        self.ui = driver.UiSettings(os.path.join(self.tmp, 'ui.json'))
        self.d = driver.DbusService(self.cfg, self.c, self.ui)
        self.svc = self.d.svc
        self.t = driver.Telemetry(60)
        for can_id, frame in ((nc.ID_ELECTRIC, ELECTRIC_FRAME), (nc.ID_PARAMETER, PARAMETER_FRAME),
                              (nc.ID_VERSION, VERSION_FRAME)):
            self.t.feed(can_id, frame)

    def refresh(self, telemetry=None):
        self.d.refresh((telemetry or self.t).snapshot(), self.c.snapshot())

    def channels(self, kind):
        return sorted({p.split('/')[2] for p in self.svc.paths if p.startswith('/%s/' % kind)})

    def value(self, path):
        return self.svc.paths[path]['value'] if path in self.svc.paths else None

    def test_service_name_and_registration(self):
        self.assertTrue(self.svc.registered)
        self.assertEqual(self.svc.name, 'com.victronenergy.dcdc.nomadic_auto')

    def test_connection_shows_the_port_auto_found(self):
        self.assertTrue(self.value('/Mgmt/Connection').startswith('CAN auto'))
        self.cfg['can']['interface'] = 'vecan1'   # what AutoCanBus does once it hears the converter
        self.refresh()
        self.assertTrue(self.value('/Mgmt/Connection').startswith('CAN vecan1'))

    def test_every_item_meets_the_gui_v2_rules(self):
        self.refresh(driver.Telemetry(1))   # offline: nothing received yet
        for kind in ('GenericInput', 'SwitchableOutput'):
            for ch in self.channels(kind):
                base = '/%s/%s' % (kind, ch)
                with self.subTest(item=base):
                    self.assertTrue(self.svc[base + '/Name'])
                    item_type = self.svc[base + '/Settings/Type']
                    self.assertTrue(self.svc[base + '/Settings/ValidTypes'] & (1 << item_type))
                    labels = self.value(base + '/Settings/Labels')
                    if labels is not None:
                        self.assertTrue(labels and all(isinstance(x, str) for x in labels))
                    if kind == 'SwitchableOutput':   # needs a valid State or Dimming
                        self.assertTrue(self.value(base + '/State') is not None
                                        or self.value(base + '/Dimming') is not None)

    def test_offline_shows_no_communication(self):
        self.refresh(driver.Telemetry(1))
        self.assertEqual(self.svc['/GenericInput/status/Value'], driver.NO_COMMUNICATION)
        self.assertEqual(driver.MODE_LABELS[driver.NO_COMMUNICATION], 'No communication')
        self.assertIsNone(self.svc['/GenericInput/lv_voltage/Value'])
        self.assertEqual(self.svc['/SwitchableOutput/direction/Status'], driver.STATUS_FAULT)

    def test_readings_and_alarms(self):
        self.refresh()
        gi = lambda ch, leaf='Value': self.svc['/GenericInput/%s/%s' % (ch, leaf)]
        self.assertEqual((gi('lv_voltage'), gi('hv_current'), gi('temperature')), (13.6, -5, 35))
        self.assertEqual(driver.MODE_LABELS[gi('status')], 'Buck 48V to 12V')
        self.assertEqual(driver.ALARM_LABELS[gi('alarm')], 'CAN loss')
        self.assertEqual(gi('alarm', 'Status'), 1)
        self.assertEqual(gi('alarm', 'Settings/PrimaryLabel'), '5 active')
        self.assertEqual(self.svc['/Dc/0/Voltage'], 13.6)
        self.assertEqual(self.svc['/FirmwareVersion'], 0x123)

    def test_controls_and_their_limits(self):
        self.refresh()
        so = '/SwitchableOutput/'
        self.assertEqual(driver.DIRECTION_LABELS[self.svc[so + 'direction/Dimming']], 'Automatic')
        self.assertEqual(self.svc[so + 'buck_voltage/Settings/DimmingMax'], 13.5)
        self.assertEqual(self.svc[so + 'buck_current_limit/Settings/DimmingMax'], 120)
        self.assertEqual(self.svc[so + 'boost_current_limit/Settings/DimmingMax'], 23)
        self.assertEqual(self.svc[so + 'buck_voltage/Settings/Group'], 'DC-DC D1 settings')
        self.assertIsNone(self.svc[so + 'direction/Settings/Group'])
        # precharge is hidden by default (the D1 manual doesn't use it)
        self.assertEqual(self.svc[so + 'precharge/Settings/ShowUIControl'], 0)
        self.assertEqual(self.svc['/GenericInput/precharge_status/Settings/ShowUIInput'], 0)

    def test_writes_from_the_screen_or_vrm(self):
        so = '/SwitchableOutput/'
        self.assertEqual(self.svc.SetValue(so + 'direction/Dimming', 1), 0)     # index 1 = Off
        self.assertEqual(self.c.snapshot()['direction'], 0)
        self.refresh()
        self.assertEqual(self.svc['/Mode'], 4)
        self.assertEqual(self.svc.SetValue(so + 'direction/Dimming', 2), 0)     # Buck
        self.assertEqual(nc.decode_ctrl(self.c.frame())['direction'], 1)
        self.assertEqual(self.svc.SetValue(so + 'buck_voltage/Dimming', 13.35), 0)
        self.assertEqual(self.svc.SetValue(so + 'buck_voltage/Dimming', 13.6), 2)   # over the limit
        self.assertEqual(self.svc.SetValue(so + 'buck_current_limit/Dimming', 40.0), 0)
        self.assertEqual(self.c.snapshot()['buck_current_limit'], 40)
        self.assertEqual(self.svc.SetValue(so + 'boost_current_limit/Dimming', 58), 2)
        self.assertEqual(self.svc.SetValue(so + 'direction/Dimming', 4), 2)     # no such option
        # The device-list on/off switch: off, then on returns to the last choice
        self.refresh()   # the screen shows /Mode = on (Buck) before it's switched
        self.assertEqual(self.svc.SetValue('/Mode', 4), 0)
        self.assertEqual(self.c.snapshot()['direction'], 0)
        self.assertEqual(self.svc.SetValue('/Mode', 1), 0)
        self.assertEqual(self.c.snapshot()['direction'], 1)
        # Automatic over D-Bus is -1, and then nothing is sent
        self.assertEqual(self.svc.SetValue('/Nomadic/Control/Direction', -1), 0)
        self.assertIsNone(self.c.frame())

    def test_changes_from_elsewhere_are_mirrored(self):
        self.c.update({'boost_voltage': 54.0, 'direction': 0})   # e.g. from the web page
        self.refresh()
        self.assertEqual(self.svc['/SwitchableOutput/boost_voltage/Dimming'], 54.0)
        self.assertEqual(self.svc['/SwitchableOutput/direction/Dimming'], 1)

    def test_renaming_and_hiding_survive_a_restart(self):
        so = '/SwitchableOutput/buck_current_limit/Settings/'
        self.assertEqual(self.svc.SetValue('/GenericInput/lv_voltage/Settings/CustomName', '12V battery'), 0)
        self.assertEqual(self.svc.SetValue(so + 'ShowUIControl', 2), 0)
        self.assertEqual(self.svc.SetValue(so + 'Group', 'x' * 33), 2)   # longer than 32 bytes
        again = driver.DbusService(self.cfg, self.c, driver.UiSettings(os.path.join(self.tmp, 'ui.json')))
        self.assertEqual(again.svc['/GenericInput/lv_voltage/Settings/CustomName'], '12V battery')
        self.assertEqual(again.svc[so + 'ShowUIControl'], 2)

    def test_switch_pane_can_be_turned_off(self):
        self.cfg['dbus']['switch_pane'] = 'no'
        d = driver.DbusService(self.cfg, self.c, self.ui)
        d.refresh(self.t.snapshot(), self.c.snapshot())
        self.assertFalse(any(p.startswith(('/GenericInput', '/SwitchableOutput')) for p in d.svc.paths))


if __name__ == '__main__':
    unittest.main()
