#!/usr/bin/env python3
"""Venus OS driver for the Nomadic D1 bidirectional DC-DC converter (CAN).

Runs on a Victron GX device (Cerbo GX, Ekrano GX, ...) and:
  * receives DCDC_PARAMETER, DCDC_ELECTRIC_VALUE and DCDC_VERSION and publishes
    them on D-Bus, so they appear in the GX device list, VRM, MQTT and Node-RED
  * sends EMS_DCDC_CTRL every 10 ms with the requested direction, set-points,
    current limit and precharge request
  * serves a small web page for monitoring and control

Usage:
  dbus-nomadic-dcdc.py              run as a service (D-Bus + web page)
  dbus-nomadic-dcdc.py --monitor    listen only: print decoded values, send nothing
  dbus-nomadic-dcdc.py --no-dbus    control + web page without D-Bus (bench test
                                    on any Linux PC with a SocketCAN adapter)
"""

import argparse
import base64
import configparser
import errno
import hmac
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nomadic_can as nc

VERSION = '1.5.1'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VELIB_PATHS = (
    os.path.join(BASE_DIR, 'ext', 'velib_python'),
    '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python',
)
TX_PERIOD = 0.010  # EMS_DCDC_CTRL period from the spec
SYS_NET = '/sys/class/net'
VERSION_FILE = '/opt/victronenergy/version'

log = logging.getLogger('nomadic-dcdc')

DEFAULTS = {
    'can': {
        'interface': 'auto',
        'timeout': '1.0',
    },
    'control': {
        'startup': 'auto',
        'automatic': 'limited',
        'test_timer': '0',
        'direction_change_delay': '1.0',
        # Ranges from the D1 manual (Rev D), "Configurable Parameters"
        'buck_voltage': '13.5',
        'buck_voltage_min': '10.0',
        'buck_voltage_max': '13.5',
        'boost_voltage': '55.2',
        'boost_voltage_min': '42.0',
        'boost_voltage_max': '57.8',
        # Current limits: the same safe values as the shipped config.ini (stock LTV1: 2 AWG cable on a
        # 150 A fuse), so a missing or damaged config.ini never falls back to the D1's factory
        # 178 A / 57 A. Raise them only in config.ini, and only for upgraded cable and fuses.
        'buck_current_limit': '100',
        'buck_current_limit_max': '120',
        'boost_current_limit': '20',
        'boost_current_limit_max': '23',
        'precharge_voltage': '48.0',
    },
    'dbus': {
        'service_class': 'dcdc',
        'device_instance': '70',
        'custom_name': 'DC-DC D1',
        'update_interval': '1.0',
        'switch_pane': 'yes',
    },
    'web': {
        'port': '8088',
        'bind': '0.0.0.0',
        'password': '',
    },
}

# Direction as the driver sees it. AUTO is not a CAN value. In AUTO with automatic = limited
# (the default) the driver chooses Off, Buck or Boost itself with the D1 manual's rules (AutoLogic)
# and sends the current limits; with automatic = hands_off it sends no EMS_DCDC_CTRL at all, so
# the D1 runs on its own built-in buck/boost logic at its factory limits.
DIRECTION_AUTO = -1
AUTOMATIC_CHOICES = ('limited', 'hands_off')
DIRECTION_NAMES = {DIRECTION_AUTO: 'Automatic'}
DIRECTION_NAMES.update(nc.DIRECTION_NAMES)
STARTUP_CHOICES = {'auto': DIRECTION_AUTO, 'off': nc.DIRECTION_OFF}


def load_config(path):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read_dict(DEFAULTS)
    if os.path.exists(path):
        cfg.read(path)
    else:
        log.warning('%s not found, using built-in defaults', path)
    return cfg


def load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning('Could not read %s: %s', path, e)
        return {}


def save_json(path, data):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log.warning('Could not save %s: %s', path, e)


# --------------------------------------------------------------------------- control

class AutoLogic:
    """Automatic with limits: the D1 manual's own buck/boost rules, decided on the Cerbo.

    Left alone, the D1 picks buck, boost or standby from its 12 V-side voltage, at its factory
    current limits. Here the driver makes the same choice from the D1's own readings
    (DCDC_ELECTRIC_VALUE) and sends it in EMS_DCDC_CTRL together with the configured current
    limits, so the owner never has to pick Buck or Boost. Thresholds: D1 manual Rev D, section VI
    and the "Quick mode trigger reference". Two differences, both on the careful side:

      * a condition must hold for CONFIRM seconds before the direction changes, so a short dip
        (the engine cranking) or a noisy reading doesn't start a buck or end a boost;
      * after boost stops because the 12 V side sagged, boost isn't tried again for BOOST_RETRY
        seconds, so a weak alternator can't make it flap between buck and boost.

    Only one of Off, Buck or Boost is ever chosen; the Controller still adds the Off pause
    whenever the direction reverses.
    """

    BUCK_BELOW = 12.5        # V, 12 V side: start buck below this ...
    HV_MIN, HV_MAX = 41.0, 58.0   # ... while the 48 V side is in this window
    BUCK_EXIT_ABOVE = 13.5   # V, 12 V side: stop buck above this
    BOOST_FROM = 13.5        # V, 12 V side: start boost from this ...
    LV_MAX = 16.0            # ... up to this (above it: never convert)
    BOOST_STOP_BELOW = 12.3  # V, 12 V side: stop boost below this
    BUCK_TARGET_MAX = 13.4   # V: the manual's own buck target. Kept below BUCK_EXIT_ABOVE, or
                             # buck would push the 12 V side over its own exit threshold.
    CONFIRM = 2.0            # s a condition must hold before acting on it
    BOOST_RETRY = 60.0       # s before boost may start again after a low-voltage stop

    def __init__(self):
        self.reset()

    def reset(self):
        """Start again from standby (when Automatic is chosen again after Off, Buck or Boost)."""
        self.direction = nc.DIRECTION_OFF
        self.reason = 'Waiting for readings from the converter'
        self._since = {}
        self._boost_blocked_until = None

    def _held(self, name, condition, now):
        """True once `condition` has been true continuously for CONFIRM seconds."""
        if not condition:
            self._since.pop(name, None)
            return False
        return now - self._since.setdefault(name, now) >= self.CONFIRM

    def _set(self, direction, reason):
        if direction != self.direction:
            log.info('Automatic: %s (%s)', DIRECTION_NAMES[direction], reason)
            self._since.clear()
        self.direction, self.reason = direction, reason
        return direction

    def decide(self, electric, now):
        """The direction to send now, from the latest DCDC_ELECTRIC_VALUE (None if stale)."""
        if electric is None:
            self._since.clear()
            return self._set(nc.DIRECTION_OFF, 'no readings from the converter, so Off')
        lv, hv = electric['lv_voltage'], electric['hv_voltage']
        buck_ok = self.HV_MIN <= hv <= self.HV_MAX
        d = self.direction
        if d == nc.DIRECTION_BUCK:
            if not buck_ok:
                return self._set(nc.DIRECTION_OFF, '48 V side %.1f V is outside %g-%g V'
                                 % (hv, self.HV_MIN, self.HV_MAX))
            if self._held('buck_exit', lv > self.BUCK_EXIT_ABOVE, now):
                return self._set(nc.DIRECTION_OFF, '12 V side %.1f V is above %g V'
                                 % (lv, self.BUCK_EXIT_ABOVE))
            return self._set(d, '12 V side was below %g V' % self.BUCK_BELOW)
        if d == nc.DIRECTION_BOOST:
            if lv > self.LV_MAX:
                return self._set(nc.DIRECTION_OFF, '12 V side %.1f V is above %g V' % (lv, self.LV_MAX))
            if self._held('boost_stop', lv < self.BOOST_STOP_BELOW, now):
                self._boost_blocked_until = now + self.BOOST_RETRY
                return self._set(nc.DIRECTION_OFF, '12 V side %.1f V fell below %g V; no boost for %g s'
                                 % (lv, self.BOOST_STOP_BELOW, self.BOOST_RETRY))
            return self._set(d, '12 V side was %g V or more' % self.BOOST_FROM)
        # Standby (Off)
        if self._held('buck', lv < self.BUCK_BELOW and buck_ok, now):
            return self._set(nc.DIRECTION_BUCK, '12 V side %.1f V is below %g V' % (lv, self.BUCK_BELOW))
        blocked = self._boost_blocked_until is not None and now < self._boost_blocked_until
        if not blocked and self._held('boost', self.BOOST_FROM <= lv <= self.LV_MAX, now):
            return self._set(nc.DIRECTION_BOOST, '12 V side %.1f V is %g V or more'
                             % (lv, self.BOOST_FROM))
        if lv < self.BUCK_BELOW and not buck_ok:
            why = '12 V side is low, but the 48 V side %.1f V is outside %g-%g V' % (
                hv, self.HV_MIN, self.HV_MAX)
        else:
            why = '12 V side %.1f V' % lv
        if blocked:
            why += '; boost paused after the 12 V side sagged (%d s left)' % (
                self._boost_blocked_until - now)
        return self._set(nc.DIRECTION_OFF, 'Standby: ' + why)


class Controller:
    """The commanded state, shared by the TX thread, D-Bus and the web page."""

    WRITABLE = ('direction', 'buck_voltage', 'boost_voltage', 'buck_current_limit',
                'boost_current_limit', 'precharge', 'precharge_voltage')
    PERSISTED = ('direction', 'last_direction', 'buck_voltage', 'boost_voltage',
                 'buck_current_limit', 'boost_current_limit', 'precharge_voltage')
    CURRENT_FIELDS = ('buck_current_limit', 'boost_current_limit')

    def __init__(self, cfg, state_path):
        c = cfg['control']
        self.limits = {
            'buck_voltage': (c.getfloat('buck_voltage_min'), c.getfloat('buck_voltage_max')),
            'boost_voltage': (c.getfloat('boost_voltage_min'), c.getfloat('boost_voltage_max')),
            'buck_current_limit': (0, c.getint('buck_current_limit_max')),
            'boost_current_limit': (0, c.getint('boost_current_limit_max')),
            'precharge_voltage': (nc.PRECHARGE_VOLTAGE_MIN, nc.PRECHARGE_VOLTAGE_MAX),
        }
        for field in ('buck_voltage', 'boost_voltage'):
            lo, hi = self.limits[field]
            if not 0 <= lo <= hi <= nc.VOLTAGE_MAX:
                raise ValueError('config: %s limits must satisfy 0 <= min <= max <= %.2f'
                                 % (field, nc.VOLTAGE_MAX))
        for field in self.CURRENT_FIELDS:
            if not 0 <= self.limits[field][1] <= nc.CURRENT_LIMIT_MAX:
                raise ValueError('config: %s_max must be 0..%d' % (field, nc.CURRENT_LIMIT_MAX))

        startup = c.get('startup').strip().lower()
        if startup not in ('auto', 'off', 'last'):
            raise ValueError('config: startup must be auto, off or last')
        self.automatic = c.get('automatic').strip().lower()
        if self.automatic not in AUTOMATIC_CHOICES:
            raise ValueError('config: automatic must be %s' % ' or '.join(AUTOMATIC_CHOICES))
        self.auto = AutoLogic() if self.automatic == 'limited' else None
        self.telemetry = None   # set by main(); AutoLogic reads the converter's voltages from it

        self.change_delay = c.getfloat('direction_change_delay')
        self.test_timer = c.getfloat('test_timer') * 60   # seconds; 0 = off
        self._active_since = None
        self.state_path = state_path
        self._lock = threading.Lock()
        self._last_active = 0
        self._last_active_time = 0.0
        self._sent_direction = None

        defaults = {
            'direction': STARTUP_CHOICES.get(startup, DIRECTION_AUTO),
            'last_direction': DIRECTION_AUTO,
            'buck_voltage': c.get('buck_voltage'),
            'boost_voltage': c.get('boost_voltage'),
            'buck_current_limit': c.get('buck_current_limit'),
            'boost_current_limit': c.get('boost_current_limit'),
            'precharge': False,
            'precharge_voltage': c.get('precharge_voltage'),
        }
        saved = self._load()
        if startup != 'last':
            saved.pop('direction', None)
        values = {}
        for field, default in defaults.items():
            if field in saved:
                try:
                    values[field] = self._check(field, saved[field])
                    continue
                except ValueError as e:
                    log.warning('Ignoring saved %s: %s', field, e)
            try:
                values[field] = self._check(field, default)
            except ValueError as e:
                raise ValueError('config: %s' % e)
        self._values = values
        if values['direction'] in (nc.DIRECTION_BUCK, nc.DIRECTION_BOOST):
            self._active_since = time.monotonic()
        log.info('Start-up control: %s', self._describe(values))
        log.info('Automatic = %s: %s', self.automatic,
                 'the Cerbo chooses buck or boost by the D1 manual\'s rules and sends the current '
                 'limits' if self.auto else 'the Cerbo sends nothing; the D1 uses its own factory limits')
        if self.test_timer:
            log.warning('Test timer on: Buck/Boost switch to Off after %g min', self.test_timer / 60)

    def _check(self, field, value):
        """Validate and normalise one control value; raise ValueError if not allowed."""
        if field in ('direction', 'last_direction'):
            allowed = (-1, 1, 2) if field == 'last_direction' else (-1, 0, 1, 2)
            if isinstance(value, str) or value not in allowed:
                raise ValueError('%s must be one of %s (-1 = automatic)' % (field, allowed))
            return int(value)
        if field == 'precharge':
            if isinstance(value, str) or value not in (0, 1):   # also accepts True/False
                raise ValueError('precharge must be 0 or 1')
            return bool(value)
        try:
            v = float(value)
            if field in self.CURRENT_FIELDS:
                if v != int(v):
                    raise ValueError
                v = int(v)
            else:
                v = round(v, 3)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('%s: invalid value %r' % (field, value))
        lo, hi = self.limits[field]
        if not lo <= v <= hi:   # also rejects NaN
            raise ValueError('%s must be between %g and %g' % (field, lo, hi))
        return v

    def update(self, changes):
        """Apply a dict of changes atomically. Raises ValueError and changes nothing on error."""
        if not isinstance(changes, dict) or not changes:
            raise ValueError('expected an object with one or more of: %s' % ', '.join(self.WRITABLE))
        unknown = set(changes) - set(self.WRITABLE)
        if unknown:
            raise ValueError('unknown setting(s): %s' % ', '.join(sorted(unknown)))
        with self._lock:
            new = dict(self._values)
            for field, value in changes.items():
                new[field] = self._check(field, value)
            if new['direction'] != nc.DIRECTION_OFF:
                new['last_direction'] = new['direction']
            if (self.auto is not None and new['direction'] == DIRECTION_AUTO
                    and self._values['direction'] != DIRECTION_AUTO):
                self.auto.reset()   # decide afresh, not from whatever Automatic last did
            if new['direction'] not in (nc.DIRECTION_BUCK, nc.DIRECTION_BOOST):
                self._active_since = None
            elif new['direction'] != self._values['direction']:
                self._active_since = time.monotonic()
            self._values = new
            self._save(new)
        log.info('Control changed: %s', self._describe(changes))

    def frame(self):
        """EMS_DCDC_CTRL payload for this TX cycle, or None in hands-off automatic (send nothing)."""
        electric = None
        if self.auto is not None and self.telemetry is not None:
            electric = self.telemetry.fresh(nc.ID_ELECTRIC)
        with self._lock:
            v = self._values
            now = time.monotonic()
            automatic = v['direction'] == DIRECTION_AUTO
            if automatic:
                if self.auto is None:
                    self._sent_direction = None
                    return None
                direction = self.auto.decide(electric, now)
            else:
                if (self.test_timer and self._active_since is not None
                        and now - self._active_since > self.test_timer):
                    log.warning('Test timer: %s ran for %g min, switching to Off',
                                DIRECTION_NAMES[v['direction']], self.test_timer / 60)
                    v = self._values = dict(v, direction=nc.DIRECTION_OFF)
                    self._active_since = None
                    self._save(v)
                direction = v['direction']
            chosen = direction
            # Never reverse buck <-> boost without direction_change_delay seconds of OFF between.
            if (direction and self._last_active and direction != self._last_active
                    and now - self._last_active_time < self.change_delay):
                direction = nc.DIRECTION_OFF
            if direction:
                self._last_active, self._last_active_time = direction, now
            self._sent_direction = direction
            # The set-point and current limit apply to whichever side is the output.
            mode = chosen if automatic else (v['direction'] or v['last_direction'])
            side = 'boost' if mode == nc.DIRECTION_BOOST else 'buck'
            voltage = v[side + '_voltage']
            if automatic and side == 'buck':
                voltage = min(voltage, AutoLogic.BUCK_TARGET_MAX)
            return nc.encode_ctrl(direction, v['precharge'], v[side + '_current_limit'],
                                  voltage, v['precharge_voltage'])

    def snapshot(self):
        with self._lock:
            out = dict(self._values)
            out['sent_direction'] = self._sent_direction
            out['automatic'] = self.automatic
            out['auto_reason'] = (self.auto.reason if self.auto is not None
                                  and out['direction'] == DIRECTION_AUTO else None)
            out['test_timer_left'] = (
                None if self._active_since is None or not self.test_timer
                else max(0, round(self.test_timer - (time.monotonic() - self._active_since))))
        out['limits'] = self.limits
        out['direction_change_delay'] = self.change_delay
        return out

    @staticmethod
    def _describe(values):
        parts = []
        for k, v in values.items():
            if k in ('direction', 'last_direction'):
                v = DIRECTION_NAMES.get(v, v)
            parts.append('%s=%s' % (k, v))
        return ', '.join(parts)

    def _load(self):
        return load_json(self.state_path)

    def _save(self, values):
        save_json(self.state_path, {k: values[k] for k in self.PERSISTED})


# --------------------------------------------------------------------------- telemetry

class Telemetry:
    """Latest decoded frames from the converter."""

    DECODERS = {
        nc.ID_PARAMETER: nc.decode_parameter,
        nc.ID_ELECTRIC: nc.decode_electric,
        nc.ID_VERSION: nc.decode_version,
    }

    def __init__(self, timeout):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._latest = {}
        self._counts = dict.fromkeys(nc.RX_IDS, 0)
        self.bad_frames = 0

    def feed(self, can_id, data):
        decode = self.DECODERS.get(can_id)
        if decode is None:
            return
        if len(data) != 8:
            self.bad_frames += 1
            return
        value = decode(data)
        with self._lock:
            self._latest[can_id] = (time.monotonic(), value)
            self._counts[can_id] += 1

    def fresh(self, can_id):
        """The latest decoded value of one message, or None if none arrived within the timeout."""
        now = time.monotonic()
        with self._lock:
            t, value = self._latest.get(can_id, (None, None))
        return value if t is not None and now - t <= self.timeout else None

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            latest = dict(self._latest)
            counts = dict(self._counts)
        ages, fresh = {}, {}
        for can_id in nc.RX_IDS:
            t, value = latest.get(can_id, (None, None))
            ages[can_id] = None if t is None else now - t
            fresh[can_id] = value if t is not None and now - t <= self.timeout else None
        parameter, electric = fresh[nc.ID_PARAMETER], fresh[nc.ID_ELECTRIC]
        version = latest.get(nc.ID_VERSION, (None, None))[1]   # does not change; keep last
        return {
            'online': parameter is not None or electric is not None,
            'parameter': parameter,
            'electric': electric,
            'version': version,
            'ages': ages,
            'counts': counts,
        }


class StatusWatcher:
    """Logs when the converter goes on/offline and when alarms appear or clear."""

    def __init__(self, interface):
        self._interface = interface
        self.bus = None   # set to the bus, so "auto" is reported as the port it found
        self.online = None
        self.alarms = set()

    @property
    def interface(self):
        return getattr(self.bus, 'interface', None) or self._interface

    def __call__(self, snap):
        if snap['online'] != self.online:
            if snap['online']:
                log.info('Converter online on %s', self.interface)
            else:
                log.warning('Converter OFFLINE: no frames received on %s', self.interface)
            self.online = snap['online']
        alarms = set(nc.alarm_texts(snap['parameter']))
        for a in sorted(alarms - self.alarms):
            log.warning('Alarm: %s', a)
        for a in sorted(self.alarms - alarms):
            log.info('Alarm cleared: %s', a)
        self.alarms = alarms


# --------------------------------------------------------------------------- CAN I/O

class CanBus:
    """Raw SocketCAN access: one filtered RX socket and one non-blocking TX socket."""

    FRAME = struct.Struct('=IB3x8s')   # struct can_frame
    TRANSIENT = (errno.ENOBUFS, errno.EAGAIN, errno.ENETDOWN)

    def __init__(self, interface):
        self.interface = interface
        self._rx = self._socket(nc.RX_IDS)
        self._rx.settimeout(0.5)
        self._tx = None

    def _socket(self, ids):
        s = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # Standard-ID data frames with exactly these IDs; an empty list receives nothing.
        mask = socket.CAN_SFF_MASK | socket.CAN_EFF_FLAG | socket.CAN_RTR_FLAG
        filters = b''.join(struct.pack('=II', i, mask) for i in ids)
        try:
            s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, filters)
        except OSError as e:
            log.warning('Could not set CAN filter: %s', e)
        s.bind((self.interface,))
        return s

    def open_tx(self):
        self._tx = self._socket(())
        self._tx.setblocking(False)

    def recv(self):
        """Return (can_id, data), or None if nothing arrived within the timeout."""
        try:
            raw = self._rx.recv(self.FRAME.size)
        except socket.timeout:
            return None
        can_id, dlc, data = self.FRAME.unpack(raw)
        return can_id & socket.CAN_SFF_MASK, data[:dlc]

    def send(self, can_id, data):
        self._tx.send(self.FRAME.pack(can_id, len(data), data))

    def close(self):
        for s in (self._rx, self._tx):
            if s is not None:
                s.close()



AUTO = 'auto'
AUTO_MIN_FRAMES = 5   # frames with the converter's IDs needed to accept a port (it sends ~130 a second)
# A port is only accepted once 8-byte frames of both of these have been heard on it, so a stray frame
# that happens to use one of the converter's IDs on another bus can't make the driver transmit there.
AUTO_REQUIRED_IDS = (nc.ID_PARAMETER, nc.ID_ELECTRIC)


def can_ports():
    """Names of the CAN interfaces (can0, can1, vecan0, vecan1 ...), from /sys/class/net."""
    try:
        names = sorted(os.listdir(SYS_NET))
    except OSError:
        return []
    return [n for n in names if _read_int(os.path.join(SYS_NET, n, 'type')) == 280]   # ARPHRD_CAN


def find_converter(seconds=1.0):
    """Listen (transmitting nothing) on each CAN port for up to `seconds` and return the first port
    the converter is heard on, or None. Only 8-byte standard-ID frames with the converter's IDs
    count, and both its status (0x066) and its readings (0x088) must be among them."""
    for name in can_ports():
        try:
            bus = CanBus(name)
        except OSError:
            continue
        heard, ids = 0, set()
        try:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                frame = bus.recv()
                if frame and frame[0] in nc.RX_IDS and len(frame[1]) == 8:
                    heard += 1
                    ids.add(frame[0])
                    if heard >= AUTO_MIN_FRAMES and ids.issuperset(AUTO_REQUIRED_IDS):
                        return name
        except OSError:
            pass
        finally:
            bus.close()
    return None


class AutoCanBus:
    """interface = auto: listens on every CAN port until the converter is heard, then uses only that
    port. Nothing is transmitted before the converter has been heard, and never on any other port."""

    SEARCH_PAUSE = 2.0

    def __init__(self, cfg, transmit):
        self.cfg, self.transmit = cfg, transmit
        self.interface = AUTO
        self._bus = None

    def _search(self):
        log.info('interface = auto: looking for the converter on %s',
                 ', '.join(can_ports()) or 'any CAN port (none found yet)')
        last_log = time.monotonic()
        while True:
            name = find_converter()
            if name:
                bus = CanBus(name)
                if self.transmit:
                    bus.open_tx()
                self.cfg['can']['interface'] = name
                self.interface = name
                self._bus = bus
                log.info('Converter found on %s; using that port', name)
                return
            if time.monotonic() - last_log >= 60:
                last_log = time.monotonic()
                log.warning('Converter still not heard on %s. Check its power, the CAN cable and '
                            'terminators, and the port\'s CAN-bus profile (250 / 500 kbit/s).',
                            ', '.join(can_ports()) or 'any CAN port')
            time.sleep(self.SEARCH_PAUSE)

    def recv(self):
        if self._bus is None:
            self._search()
        return self._bus.recv()

    def send(self, can_id, data):
        bus = self._bus
        if bus is None:
            return False   # the converter hasn't been heard yet: send nothing
        bus.send(can_id, data)
        return True

    def close(self):
        if self._bus is not None:
            self._bus.close()


class SimulatedD1:
    """A pretend D1 for --simulate. Nothing touches a CAN bus.

    It follows the D1 manual's automatic logic (buck below 12.5 V while the 48 V side is 41-58 V,
    standby between 12.5 and 13.5 V, boost from 13.5 V until the 12 V side falls below 12.3 V) at
    its factory limits, and obeys EMS_DCDC_CTRL while that keeps arriving. With engine=True the
    pretend 12 V side alternates every three minutes between loads only and a running engine
    whose alternator holds it at 14.2 V (up to ALTERNATOR A), so every mode shows up. It is for
    trying out the screens and controls, not for predicting what the real converter will do.
    """

    interface = 'simulated'
    CONTROL_TIMEOUT = 1.0   # s without EMS_DCDC_CTRL before the pretend D1 flags CAN loss (a guess)
    LOAD = 15.0             # A drawn by the pretend 12 V loads
    ALTERNATOR = 150.0      # A the pretend alternator can give while the engine runs
    ALTERNATOR_V = 14.2     # V it regulates the 12 V side to
    PHASE = 180.0           # s engine off, then s engine on, alternately
    K_LV = 0.0005           # V per A*s on the pretend 12 V battery
    K_HV = 0.0002           # V per A*s on the pretend 48 V battery
    GAIN = 200.0            # A per V of error when regulating a voltage
    PERIODS = {nc.ID_ELECTRIC: 0.010, nc.ID_PARAMETER: 0.050, nc.ID_VERSION: 0.100}

    def __init__(self, engine=True):
        self._lock = threading.Lock()
        self.engine = engine
        self.lv, self.hv, self.temp = 12.9, 51.2, 30.0
        self.lv_i = self.hv_i = 0.0
        self.mode, self.auto_state, self.precharge = 0x2, 'standby', 0x2
        self.ctrl, self.ctrl_time, self.can_loss_until = None, 0.0, 0.0
        now = time.monotonic()
        self._start = now
        self._due = dict.fromkeys(self.PERIODS, now)
        self._last_step = now

    def send(self, can_id, data):
        if can_id == nc.ID_CTRL:
            with self._lock:
                self.ctrl, self.ctrl_time = nc.decode_ctrl(data), time.monotonic()

    def recv(self):
        """The next frame when it's due, like a socket with a 0.5 s timeout."""
        can_id = min(self._due, key=self._due.get)
        delay = self._due[can_id] - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 0.5))
            if delay > 0.5:
                return None
        now = time.monotonic()
        self._due[can_id] = max(self._due[can_id] + self.PERIODS[can_id], now - 1.0)
        with self._lock:
            self.step(now)
            return can_id, self._frame(can_id, now)

    def close(self):
        pass

    def engine_running(self, now):
        return self.engine and int((now - self._start) // self.PHASE) % 2 == 1

    def external(self, now):
        """Net current into the 12 V battery from everything but the D1."""
        if self.engine_running(now):
            return max(0.0, min(self.ALTERNATOR, (self.ALTERNATOR_V - self.lv) * self.GAIN)) - self.LOAD
        return -self.LOAD

    def step(self, now):
        dt = min(max(now - self._last_step, 0.0), 1.0)
        self._last_step = now
        if self.ctrl is not None and now - self.ctrl_time > self.CONTROL_TIMEOUT:
            self.ctrl, self.can_loss_until = None, now + 10.0   # commands stopped
        buck_ok = 41.0 <= self.hv <= 58.0
        external = self.external(now)
        lv_i = hv_i = 0.0
        if self.ctrl is not None:
            c = self.ctrl
            limit, target = c['current_limit'] or 0, c['voltage_setpoint'] or 0.0
            if c['direction'] == nc.DIRECTION_BUCK and buck_ok:
                self.mode = 0x4
                lv_i = max(0.0, min(limit, -external + (target - self.lv) * self.GAIN))
            elif c['direction'] == nc.DIRECTION_BOOST and self.lv >= 12.3:
                self.mode = 0x3
                hv_i = max(0.0, min(limit, (target - self.hv) * 10))
            else:
                self.mode = 0x2
            self.precharge = 0x1 if c['precharge'] else 0x2
            self.auto_state = 'standby'
        else:
            self.precharge = 0x2
            s = self.auto_state
            if s == 'buck' and (self.lv > 13.5 or not buck_ok):
                s = 'standby'
            elif s == 'boost' and self.lv < 12.3:
                s = 'standby'
            elif s == 'standby':
                if self.lv < 12.5 and buck_ok:
                    s = 'buck'
                elif 13.5 <= self.lv <= 16.0:
                    s = 'boost'
            self.auto_state = s
            if s == 'buck':   # factory limits: 13.4 V, up to 178 A
                lv_i = max(0.0, min(178.0, -external + (13.4 - self.lv) * self.GAIN))
            elif s == 'boost':   # factory limits: about 58 V, up to 57 A on the 48 V side
                hv_i = max(0.0, min(57.0, (58.0 - self.hv) * 10))
            self.mode = 0x2 if s == 'standby' else 0x5
        # positive current = into that side's battery
        if lv_i > 0:
            hv_i = -(self.lv * lv_i) / (self.hv * 0.94)
        elif hv_i > 0:
            lv_i = -(self.hv * hv_i) / (self.lv * 0.92)
        self.lv_i, self.hv_i = lv_i, hv_i
        self.lv = max(11.0, min(15.0, self.lv + (lv_i + external) * self.K_LV * dt))
        self.hv = max(44.0, min(57.5, self.hv + hv_i * self.K_HV * dt))
        target_temp = 30.0 + abs(self.lv * lv_i) / 100.0
        self.temp += (target_temp - self.temp) * dt / 60.0

    def _frame(self, can_id, now):
        if can_id == nc.ID_ELECTRIC:
            return nc.encode_electric(self.lv, self.lv_i, self.hv, self.hv_i, self.temp)
        if can_id == nc.ID_PARAMETER:
            voltage_status = (0x02 if self.lv < 12.3 else 0) | (0x04 if self.lv > 14.8 else 0)
            return nc.encode_parameter(voltage_status=voltage_status,
                                       faults=0x08 if now < self.can_loss_until else 0,
                                       operating_mode=self.mode, precharge_status=self.precharge)
        return nc.encode_version(0x100, 1)


def simulation_config(cfg):
    """Run beside a real installation without clashing: own D-Bus name, instance, web port, files."""
    cfg['can']['interface'] = 'sim'
    d = cfg['dbus']
    d['device_instance'] = str(d.getint('device_instance') + 1)
    d['custom_name'] = d['custom_name'] + ' (SIMULATED)'
    if cfg['web'].getint('port'):
        cfg['web']['port'] = str(cfg['web'].getint('port') + 1)


class TxStats:
    def __init__(self):
        self.sent = 0
        self.errors = 0
        self.last_frame = b''
        self.last_warning = 0.0


def rx_worker(bus, telemetry):
    while True:
        try:
            frame = bus.recv()
        except OSError as e:
            if e.errno != errno.ENETDOWN:
                raise
            log.warning('%s is down', bus.interface)
            time.sleep(1)
            continue
        if frame:
            telemetry.feed(*frame)


def tx_worker(bus, controller, stats):
    next_time = time.monotonic()
    while True:
        frame = controller.frame()
        stats.last_frame = frame or b''
        try:
            if frame is not None:   # None = hands-off automatic: the D1 runs on its own logic
                if bus.send(nc.ID_CTRL, frame) is not False:   # False: auto, port not found yet
                    stats.sent += 1
        except OSError as e:
            if e.errno not in CanBus.TRANSIENT:
                raise
            stats.errors += 1
            now = time.monotonic()
            if now - stats.last_warning > 30:
                stats.last_warning = now
                log.warning('CAN send failed (%s), %d frames not sent so far. Is the converter '
                            'connected and powered, with the same bitrate as %s?',
                            os.strerror(e.errno), stats.errors, bus.interface)
        next_time += TX_PERIOD
        delay = next_time - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.1:
            next_time = time.monotonic()   # fell far behind; don't burst to catch up


def start_thread(name, target, *args):
    def run():
        try:
            target(*args)
        except Exception:
            log.exception('%s thread failed; exiting so the service restarts', name)
            os._exit(1)
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- D-Bus

def victron_state(parameter):
    """Map the converter's operating mode onto the Victron charger state enum."""
    if parameter is None:
        return None
    if parameter['fault_status'] & 0b110:   # temporary or permanent failure
        return 2                            # Fault
    return {
        0x1: 245,   # Starting up
        0x2: 0,     # Off
        0x3: 11,    # Power supply (regulating)
        0x4: 11,
        0x5: 11,
        0x7: 0,
        0x8: 11,
    }.get(parameter['operating_mode'])


def _text(unit, digits):
    def fmt(path, value):
        return '---' if value is None else '%.*f%s' % (digits, value, unit)
    return fmt


# Read-only values (GenericInput) and controls (SwitchableOutput) for the GX switch pane, the GX
# device page and the VRM "Switches" panel / app widget.
# API: https://github.com/victronenergy/venus/wiki/dbus#switch
INPUT_DISCRETE = 0      # GenericInput: integer shown as text through /Settings/Labels
INPUT_VALUE = 1         # GenericInput: number with a unit
OUTPUT_TOGGLE = 1       # SwitchableOutput: on/off via /State
OUTPUT_DROPDOWN = 6     # SwitchableOutput: option index in /Dimming
OUTPUT_NUMERIC = 8      # SwitchableOutput: number in /Dimming
SHOW_EVERYWHERE = 1     # /Settings/ShowUIControl default: GX screen, VRM and apps
SHOW_LOCAL_REMOTE = 6   # /Settings/ShowUIInput default from the API
STATUS_OFF, STATUS_FAULT, STATUS_ON = 0x00, 0x08, 0x09

NO_COMMUNICATION = 16   # extra entry after the 16 operating modes
# Short operating-mode texts that fit a GX tile (D1 manual wording: "Standby" for idle)
SHORT_MODES = {0x1: 'Starting up', 0x2: 'Standby', 0x3: 'Boost 12V to 48V', 0x4: 'Buck 48V to 12V',
               0x5: 'Autonomous', 0x7: 'Shutting down', 0x8: 'Derating (hot)', 0xF: 'Invalid'}
MODE_LABELS = [SHORT_MODES.get(m, 'Reserved') for m in range(16)] + ['No communication']
PRECHARGE_LABELS = ['Reserved', 'In progress', 'Not active', 'Invalid']
ALARM_LABELS = ['None'] + [text for _, _, text in nc.ALARMS]
DIRECTION_LABELS = ['Automatic', 'Off', 'Buck 48V to 12V', 'Boost 12V to 48V']   # index = direction + 1

# (channel, name, type, unit, decimals, labels). Shown on the device's own card, sorted by name.
INPUTS = (
    ('alarm', 'Alarm', INPUT_DISCRETE, None, None, ALARM_LABELS),
    ('status', 'Status', INPUT_DISCRETE, None, None, MODE_LABELS),
    ('lv_voltage', 'LV voltage', INPUT_VALUE, 'V', 1, None),
    ('lv_current', 'LV current', INPUT_VALUE, 'A', 0, None),
    ('hv_voltage', 'HV voltage', INPUT_VALUE, 'V', 1, None),
    ('hv_current', 'HV current', INPUT_VALUE, 'A', 0, None),
    ('temperature', 'Temperature', INPUT_VALUE, '/Temperature', 0, None),
    ('precharge_status', 'Precharge status', INPUT_DISCRETE, None, None, PRECHARGE_LABELS),
)
# The D1 manual doesn't use precharge, so its items start hidden (show them from the device page).
HIDDEN_BY_DEFAULT = ('precharge_status', 'precharge', 'precharge_voltage')

# Direction goes on the device's card next to the readings; the rest on a "<name> settings" card.
OUTPUTS = (
    dict(channel='direction', name='Direction', type=OUTPUT_DROPDOWN, field='direction',
         labels=DIRECTION_LABELS, offset=1),
    dict(channel='buck_voltage', name='Buck voltage', type=OUTPUT_NUMERIC, field='buck_voltage',
         settings_card=True, unit='V', step=0.05, decimals=2),
    dict(channel='boost_voltage', name='Boost voltage', type=OUTPUT_NUMERIC, field='boost_voltage',
         settings_card=True, unit='V', step=0.1, decimals=1),
    dict(channel='buck_current_limit', name='Buck current limit', type=OUTPUT_NUMERIC,
         field='buck_current_limit', settings_card=True, unit='A', step=1, decimals=0),
    dict(channel='boost_current_limit', name='Boost current limit', type=OUTPUT_NUMERIC,
         field='boost_current_limit', settings_card=True, unit='A', step=1, decimals=0),
    dict(channel='precharge', name='Precharge request', type=OUTPUT_TOGGLE, field='precharge',
         settings_card=True),
    dict(channel='precharge_voltage', name='Precharge voltage', type=OUTPUT_NUMERIC,
         field='precharge_voltage', settings_card=True, unit='V', step=0.1, decimals=1),
)


class UiSettings:
    """Switch-pane names, groups and visibility as changed on the GX, kept across restarts."""

    def __init__(self, path):
        self.path = path
        self.values = load_json(path)

    def get(self, key, default):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        save_json(self.path, self.values)


class DbusService:
    """Publishes the converter as com.victronenergy.<service_class>.nomadic_<interface>.

    Dc/0 is the LV (12 V) side, Dc/In the HV (48 V) side, in both directions.
    """

    CONTROL_PATHS = {
        '/Nomadic/Control/Direction': 'direction',
        '/Nomadic/Control/BuckVoltage': 'buck_voltage',
        '/Nomadic/Control/BoostVoltage': 'boost_voltage',
        '/Nomadic/Control/BuckCurrentLimit': 'buck_current_limit',
        '/Nomadic/Control/BoostCurrentLimit': 'boost_current_limit',
        '/Nomadic/Control/Precharge': 'precharge',
        '/Nomadic/Control/PrechargeVoltage': 'precharge_voltage',
    }

    def __init__(self, cfg, controller, ui_settings):
        for p in VELIB_PATHS:
            if os.path.isdir(p):
                sys.path.insert(1, p)
                break
        from vedbus import VeDbusService

        self.controller = controller
        self.ui = ui_settings
        self.control_paths = dict(self.CONTROL_PATHS)
        self.path_offsets = {}   # path value = field value + offset
        d = cfg['dbus']
        iface = cfg['can']['interface']
        name = 'com.victronenergy.%s.nomadic_%s' % (
            d['service_class'], re.sub(r'[^A-Za-z0-9_]', '_', iface))
        try:
            svc = VeDbusService(name, register=False)
            needs_register = True
        except TypeError:   # velib from before 2024 registers immediately
            svc = VeDbusService(name)
            needs_register = False
        self.svc = svc

        svc.add_path('/Mgmt/ProcessName', os.path.abspath(__file__))
        svc.add_path('/Mgmt/ProcessVersion', VERSION)
        self.can_cfg = cfg['can']
        svc.add_path('/Mgmt/Connection', 'CAN ' + iface)
        svc.add_path('/DeviceInstance', d.getint('device_instance'))
        svc.add_path('/ProductId', 0xFFFF)
        svc.add_path('/ProductName', 'Nomadic D1 DC-DC converter')
        svc.add_path('/CustomName', d['custom_name'])
        svc.add_path('/FirmwareVersion', None)
        svc.add_path('/HardwareVersion', None)
        svc.add_path('/Connected', 1)

        V, A, W, C = _text('V', 2), _text('A', 0), _text('W', 0), _text('C', 0)
        for path, fmt in (('/Dc/0/Voltage', V), ('/Dc/0/Current', A), ('/Dc/0/Power', W),
                          ('/Dc/0/Temperature', C), ('/Dc/In/V', V), ('/Dc/In/I', A),
                          ('/Dc/In/P', W)):
            svc.add_path(path, None, gettextcallback=fmt)
        svc.add_path('/State', None)
        svc.add_path('/Mode', 4, writeable=True, onchangecallback=self._on_mode)

        for path in ('/Nomadic/Online', '/Nomadic/OperatingMode', '/Nomadic/OperatingModeText',
                     '/Nomadic/PrechargeStatus', '/Nomadic/PrechargeStatusText',
                     '/Nomadic/LoadStatus', '/Nomadic/FaultStatus', '/Nomadic/VoltageStatus',
                     '/Nomadic/Faults', '/Nomadic/Alarms', '/Nomadic/SoftwareVersion',
                     '/Nomadic/Control/SentDirection', '/Nomadic/Control/AutoReason'):
            svc.add_path(path, None)
        for path in self.CONTROL_PATHS:
            svc.add_path(path, None, writeable=True, onchangecallback=self._on_control)

        self.switch_pane = d.getboolean('switch_pane')
        if self.switch_pane:
            for spec in INPUTS:
                self._add_input(*spec)
            settings_group = '%s settings' % d['custom_name']
            for spec in OUTPUTS:
                self._add_output(spec, settings_group)

        if needs_register:
            svc.register()
        log.info('Registered %s on D-Bus', name)

    def _add_input(self, channel, name, input_type, unit, decimals, labels):
        base = '/GenericInput/' + channel
        add = self.svc.add_path
        add(base + '/Name', name)
        add(base + '/Value', None)
        add(base + '/Status', 0)
        add(base + '/Settings/Type', input_type)
        add(base + '/Settings/ValidTypes', 1 << input_type)
        if unit:
            add(base + '/Settings/Unit', unit)
        if decimals is not None:
            add(base + '/Settings/Decimals', decimals)
        if labels:
            add(base + '/Settings/Labels', labels)
        if channel == 'alarm':
            add(base + '/Settings/PrimaryLabel', None)   # "3 active" when there are several
        self._add_ui_setting(base + '/Settings/CustomName', None)
        self._add_ui_setting(base + '/Settings/Group', None)
        self._add_ui_setting(base + '/Settings/ShowUIInput',
                             0 if channel in HIDDEN_BY_DEFAULT else SHOW_LOCAL_REMOTE)

    def _add_output(self, spec, settings_group):
        base = '/SwitchableOutput/' + spec['channel']
        add = self.svc.add_path
        add(base + '/Name', spec['name'])
        add(base + '/Status', STATUS_OFF)
        value_path = base + ('/State' if spec['type'] == OUTPUT_TOGGLE else '/Dimming')
        add(value_path, None, writeable=True, onchangecallback=self._on_control)
        self.control_paths[value_path] = spec['field']
        self.path_offsets[value_path] = spec.get('offset', 0)
        add(base + '/Settings/Type', spec['type'])
        add(base + '/Settings/ValidTypes', 1 << spec['type'])
        if 'unit' in spec:
            lo, hi = self.controller.limits[spec['field']]
            add(base + '/Settings/DimmingMin', lo)
            add(base + '/Settings/DimmingMax', hi)
            add(base + '/Settings/StepSize', spec['step'])
            add(base + '/Settings/Decimals', spec['decimals'])
            add(base + '/Settings/Unit', spec['unit'])
        if 'labels' in spec:
            add(base + '/Settings/Labels', spec['labels'])
            add(base + '/Settings/DimmingMin', 0)
            add(base + '/Settings/DimmingMax', len(spec['labels']) - 1)
        # On a named card the GUI prefixes the device name unless a custom name is set.
        on_settings_card = spec.get('settings_card', False)
        self._add_ui_setting(base + '/Settings/CustomName',
                             spec['name'] if on_settings_card else None)
        self._add_ui_setting(base + '/Settings/Group', settings_group if on_settings_card else None)
        self._add_ui_setting(base + '/Settings/ShowUIControl',
                             0 if spec['channel'] in HIDDEN_BY_DEFAULT else SHOW_EVERYWHERE)

    def _add_ui_setting(self, path, default):
        self.svc.add_path(path, self.ui.get(path, default), writeable=True,
                          onchangecallback=self._on_ui_setting)

    def _on_ui_setting(self, path, value):
        if path.endswith(('/CustomName', '/Group')):
            ok = value is None or (isinstance(value, str) and len(value.encode('utf-8')) <= 32)
        else:   # /ShowUIControl or /ShowUIInput bitfield
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 15
        if ok:
            self.ui.set(path, value)
        else:
            log.warning('Rejected D-Bus write %s = %r', path, value)
        return ok

    def _on_control(self, path, value):
        try:
            if path in self.path_offsets and self.path_offsets[path]:
                if isinstance(value, str):
                    raise ValueError('expected a number')
                value = value - self.path_offsets[path]
            self.controller.update({self.control_paths[path]: value})
            return True
        except ValueError as e:
            log.warning('Rejected D-Bus write %s = %r: %s', path, value, e)
            return False

    def _on_mode(self, path, value):
        if value == 1:
            direction = self.controller.snapshot()['last_direction']
        elif value == 4:
            direction = nc.DIRECTION_OFF
        else:
            return False
        try:
            self.controller.update({'direction': direction})
            return True
        except ValueError as e:
            log.warning('Rejected /Mode = %r: %s', value, e)
            return False

    def refresh(self, snap, ctrl):
        connection = 'CAN ' + self.can_cfg['interface']
        if self.svc['/Mgmt/Connection'] != connection:   # interface = auto has found its port
            self.svc['/Mgmt/Connection'] = connection
        s = self.svc
        e, p, v = snap['electric'], snap['parameter'], snap['version']

        def power(volts, amps):
            return None if e is None else round(e[volts] * e[amps])

        s['/Dc/0/Voltage'] = e and e['lv_voltage']
        s['/Dc/0/Current'] = e and e['lv_current']
        s['/Dc/0/Power'] = power('lv_voltage', 'lv_current')
        s['/Dc/0/Temperature'] = e and e['temperature']
        s['/Dc/In/V'] = e and e['hv_voltage']
        s['/Dc/In/I'] = e and e['hv_current']
        s['/Dc/In/P'] = power('hv_voltage', 'hv_current')
        s['/State'] = victron_state(p)
        s['/FirmwareVersion'] = v and v['software_raw']
        s['/HardwareVersion'] = v and v['hardware']

        s['/Nomadic/Online'] = int(snap['online'])
        s['/Nomadic/OperatingMode'] = p and p['operating_mode']
        s['/Nomadic/OperatingModeText'] = p and p['operating_mode_text']
        s['/Nomadic/PrechargeStatus'] = p and p['precharge_status']
        s['/Nomadic/PrechargeStatusText'] = p and p['precharge_status_text']
        s['/Nomadic/LoadStatus'] = p and p['load_status']
        s['/Nomadic/FaultStatus'] = p and p['fault_status']
        s['/Nomadic/VoltageStatus'] = p and p['voltage_status']
        s['/Nomadic/Faults'] = p and p['faults']
        s['/Nomadic/Alarms'] = None if p is None else '; '.join(nc.alarm_texts(p))
        s['/Nomadic/SoftwareVersion'] = v and v['software']

        s['/Mode'] = 4 if ctrl['direction'] == nc.DIRECTION_OFF else 1
        s['/Nomadic/Control/SentDirection'] = ctrl['sent_direction']
        s['/Nomadic/Control/AutoReason'] = ctrl['auto_reason']
        for path, field in self.control_paths.items():
            value = ctrl[field]
            value = int(value) if isinstance(value, bool) else value
            s[path] = value + self.path_offsets.get(path, 0)

        if self.switch_pane:
            self._refresh_switch_pane(snap, ctrl)

    def _refresh_switch_pane(self, snap, ctrl):
        s = self.svc
        e, p = snap['electric'], snap['parameter']
        alarms = nc.active_alarms(p)
        has_fault = any(i < nc.FAULT_ALARM_COUNT for i in alarms)

        s['/GenericInput/alarm/Value'] = None if p is None else (alarms[0] + 1 if alarms else 0)
        s['/GenericInput/alarm/Status'] = 1 if has_fault else 0
        s['/GenericInput/alarm/Settings/PrimaryLabel'] = (
            '%d active' % len(alarms) if len(alarms) > 1 else None)
        if not snap['online']:
            s['/GenericInput/status/Value'] = NO_COMMUNICATION
        else:
            s['/GenericInput/status/Value'] = p and p['operating_mode']
        s['/GenericInput/status/Status'] = 0 if snap['online'] else 1
        for channel in ('lv_voltage', 'lv_current', 'hv_voltage', 'hv_current', 'temperature'):
            s['/GenericInput/%s/Value' % channel] = e and e[channel]
        s['/GenericInput/precharge_status/Value'] = p and p['precharge_status']

        failed = not snap['online'] or (p is not None and p['fault_status'] & 0b110)
        s['/SwitchableOutput/direction/Status'] = (
            STATUS_FAULT if failed else
            STATUS_OFF if ctrl['direction'] == nc.DIRECTION_OFF else STATUS_ON)
        s['/SwitchableOutput/precharge/Status'] = STATUS_ON if ctrl['precharge'] else STATUS_OFF


# --------------------------------------------------------------------------- web page

class WebApp:
    def __init__(self, cfg, controller, telemetry, tx_stats):
        self.controller = controller
        self.telemetry = telemetry
        self.tx_stats = tx_stats
        self.name = cfg['dbus']['custom_name']
        self.can_cfg = cfg['can']   # live: shows the port "auto" found
        self.password = cfg['web']['password']
        # Without a password the page is view-only: anyone on the van's Wi-Fi could otherwise change
        # the mode and limits. The Cerbo screen and VRM keep working either way.
        self.read_only = not self.password
        with open(os.path.join(BASE_DIR, 'webui.html'), 'rb') as f:
            self.page = f.read()

    def status(self):
        snap = self.telemetry.snapshot()
        return {
            'name': self.name,
            'interface': self.can_cfg['interface'],
            'driver_version': VERSION,
            'read_only': self.read_only,
            'online': snap['online'],
            'electric': snap['electric'],
            'parameter': snap['parameter'],
            'alarms': nc.alarm_texts(snap['parameter']),
            'version': snap['version'],
            'rx_age': {'0x%03X' % k: None if a is None else round(a, 2)
                       for k, a in snap['ages'].items()},
            'rx_count': {'0x%03X' % k: n for k, n in snap['counts'].items()},
            'control': self.controller.snapshot(),
            'tx': {
                'frame': ' '.join('%02X' % b for b in self.tx_stats.last_frame),
                'sent': self.tx_stats.sent,
                'errors': self.tx_stats.errors,
            },
        }


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'nomadic-dcdc/' + VERSION

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), 'application/json')

        def _authorized(self):
            if not app.password:
                return True
            header = self.headers.get('Authorization', '')
            if header.startswith('Basic '):
                try:
                    password = base64.b64decode(header[6:]).decode().partition(':')[2]
                    if hmac.compare_digest(password.encode(), app.password.encode()):
                        return True
                except ValueError:
                    pass
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="DC-DC converter"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False

        def do_GET(self):
            if not self._authorized():
                return
            path = self.path.split('?')[0]
            if path == '/':
                self._send(200, app.page, 'text/html; charset=utf-8')
            elif path == '/api/status':
                self._json(200, app.status())
            else:
                self._json(404, {'error': 'not found'})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path.split('?')[0] != '/api/control':
                return self._json(404, {'error': 'not found'})
            if app.read_only:
                return self._json(403, {'error': 'This page is view-only. To change settings here, set '
                                        'a password in config.ini ([web] password = ...) and restart '
                                        'the driver. The Cerbo screen and VRM still work.'})
            # Requiring a JSON content type forces a CORS preflight, which this server never
            # answers, so other web sites open in the same browser cannot post here.
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return self._json(415, {'error': 'Content-Type must be application/json'})
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 4096:
                    raise ValueError('bad Content-Length')
                changes = json.loads(self.rfile.read(length))
                app.controller.update(changes)
            except ValueError as e:
                return self._json(400, {'error': str(e)})
            self._json(200, app.status())

    return Handler


def start_web(cfg, app):
    bind, port = cfg['web']['bind'], cfg['web'].getint('port')
    try:
        server = ThreadingHTTPServer((bind, port), make_handler(app))
    except OSError as e:
        log.error('Web page disabled: cannot listen on %s:%d (%s)', bind, port, e)
        return
    server.daemon_threads = True
    start_thread('web', server.serve_forever)
    log.info('Web page on http://%s:%d/%s', bind, port,
             '' if app.password else ' (view-only: no password set)')


# --------------------------------------------------------------------------- monitor mode

def run_monitor(interface, timeout):
    """Listen-only bring-up tool: prints decoded frames once a second and sends nothing."""
    if interface == AUTO:
        print('interface = auto: looking for the converter on every CAN port (nothing is transmitted)...')
        interface = None
        while interface is None:
            interface = find_converter()
        print('Converter found on %s.' % interface)
    bus = CanBus(interface)
    telemetry = Telemetry(timeout)
    start_thread('can-rx', rx_worker, bus, telemetry)
    print('Listening on %s (nothing is transmitted). Ctrl+C to stop.' % interface)
    previous = dict.fromkeys(nc.RX_IDS, 0)
    while True:
        time.sleep(1)
        snap = telemetry.snapshot()
        rates = ' '.join('0x%03X=%d/s' % (i, snap['counts'][i] - previous[i]) for i in nc.RX_IDS)
        previous = snap['counts']
        print('--- %s %s  %s  %s' % (interface, time.strftime('%H:%M:%S'),
                                     'ONLINE' if snap['online'] else 'NO DATA', rates))
        e, p, v = snap['electric'], snap['parameter'], snap['version']
        if e:
            print('LV %5.1f V %+4d A | HV %5.1f V %+4d A | %d C' % (
                e['lv_voltage'], e['lv_current'], e['hv_voltage'], e['hv_current'],
                e['temperature']))
        if p:
            print('Mode: %s | %s' % (p['operating_mode_text'], p['precharge_status_text']))
            print('Alarms: %s' % ('; '.join(nc.alarm_texts(p)) or 'none'))
        if v:
            print('Software %s  Hardware %d' % (v['software'], v['hardware']))
        if telemetry.bad_frames:
            print('Ignored %d frames with a length other than 8 bytes' % telemetry.bad_frames)


def venus_version():
    """Venus OS version as (major, minor, text), or None when not on a GX device."""
    try:
        with open(VERSION_FILE) as f:
            text = f.readline().strip()
    except OSError:
        return None
    m = re.match(r'v?(\d+)\.(\d+)', text)
    return (int(m.group(1)), int(m.group(2)), text) if m else (0, 0, text)


def can_link_info(name):
    """(bitrate in bit/s, CAN state) of an interface from iproute2, or (None, None) if unknown."""
    try:
        out = subprocess.run(['ip', '-details', '-json', 'link', 'show', name],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5).stdout
        info = json.loads(out)[0]['linkinfo']['info_data']
        return info.get('bittiming', {}).get('bitrate'), info.get('state')
    except (OSError, ValueError, LookupError, TypeError, subprocess.SubprocessError):
        return None, None


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def run_scan(configured, seconds=3.0):
    """Listen-only check of every CAN interface; reports where the converter is talking."""
    version = venus_version()
    if version:
        ok = version[:2] >= (3, 80)
        print('Venus OS %s %s' % (version[2], 'OK' if ok else
              '- the GX screen and VRM need v3.80 or newer (Settings > General > Firmware)'))
    net = SYS_NET
    names = []
    for name in sorted(os.listdir(net)):
        if _read_int(os.path.join(net, name, 'type')) == 280:   # ARPHRD_CAN
            names.append(name)
    if not names:
        print('No CAN ports found.')
        return
    print('Listening %g s on each CAN port (nothing is transmitted)...' % seconds)
    found, wrong_speed = [], []
    for name in names:
        bitrate, can_state = can_link_info(name)
        speed = '%d kbit/s' % (bitrate // 1000) if bitrate else 'speed ?'
        errors_path = os.path.join(net, name, 'statistics', 'rx_errors')
        errors_before = _read_int(errors_path)
        counts = dict.fromkeys(nc.RX_IDS, 0)
        try:
            bus = CanBus(name)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                frame = bus.recv()
                if frame:
                    counts[frame[0]] += 1
            bus.close()
        except OSError as e:
            print('  %-7s cannot listen: %s' % (name, e))
            continue
        errors = None
        if errors_before is not None:
            errors_after = _read_int(errors_path)
            if errors_after is not None:
                errors = errors_after - errors_before
        heard = sum(counts.values())
        if heard:
            found.append(name)
            result = 'CONVERTER FOUND (%d frames)' % heard
        elif errors and errors >= 10:
            # Frames that can't be decoded at this bitrate show up as bus errors.
            wrong_speed.append((name, bitrate))
            result = 'nothing decoded, but %d bus errors: talking at a different speed?' % errors
        else:
            result = 'nothing from the converter'
        state = can_state or _read_text(os.path.join(net, name, 'operstate')) or '?'
        print('  %-7s %-11s %-13s %s' % (name, speed, state.lower(), result))
    if len(found) == 1:
        if configured == AUTO:
            print('The converter is on %s. config.ini has interface = auto, so the driver finds it '
                  'there by itself. Good.' % found[0])
        elif found[0] == configured:
            print('The converter is on %s, which matches config.ini. Good.' % found[0])
        else:
            print('The converter is on %s. In config.ini set:  interface = %s' % (found[0], found[0]))
    elif not found and wrong_speed:
        name, bitrate = wrong_speed[0]
        other = {250000: '500', 500000: '250'}.get(bitrate, 'other')
        profile = {'250': 'VE.Can (250 kbit/s)', '500': 'CAN-bus BMS LV (500 kbit/s)'}.get(
            other, 'the other speed')
        print('Something is talking on %s at a different speed. On the Cerbo, set that port\'s CAN-bus '
              'profile to "%s" and run this again.' % (name, profile))
    elif not found:
        print('The converter was not heard on any port. Check that the D1 has KL30 and KL15 (12 V), '
              'the cable, the 120 ohm terminators, and try the other speed (250 or 500 kbit/s).')


def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='Nomadic D1 DC-DC converter driver for Venus OS')
    ap.add_argument('--config', default=os.path.join(BASE_DIR, 'config.ini'))
    ap.add_argument('--interface', help='CAN interface, overrides [can] interface in the config')
    ap.add_argument('--monitor', action='store_true',
                    help='listen only and print decoded values; transmits nothing')
    ap.add_argument('--scan', action='store_true',
                    help='listen on every CAN port and report which one the converter is on')
    ap.add_argument('--simulate', action='store_true',
                    help='run against a pretend D1; no CAN bus is used at all')
    ap.add_argument('--no-dbus', action='store_true', help='run without D-Bus (bench testing)')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(levelname)s: %(message)s')
    cfg = load_config(args.config)
    if args.interface:
        cfg['can']['interface'] = args.interface
    interface = cfg['can']['interface']
    timeout = cfg['can'].getfloat('timeout')

    if args.scan:
        run_scan(interface)
        return
    if args.monitor:
        run_monitor(interface, timeout)
        return

    files = ('state.json', 'ui.json')
    if args.simulate:
        simulation_config(cfg)
        interface = SimulatedD1.interface
        files = ('state-sim.json', 'ui-sim.json')
        signal.signal(signal.SIGINT, lambda *a: os._exit(0))
        signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
        log.warning('SIMULATION: pretend converter, nothing is sent on any CAN bus. Ctrl+C to stop.')

    log.info('Nomadic DC-DC driver %s starting on %s', VERSION, interface)
    controller = Controller(cfg, os.path.join(BASE_DIR, files[0]))
    telemetry = Telemetry(timeout)
    controller.telemetry = telemetry
    tx_stats = TxStats()
    watcher = StatusWatcher(interface)

    dbus_service = None
    if not args.no_dbus:
        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)
        dbus_service = DbusService(cfg, controller,
                                   UiSettings(os.path.join(BASE_DIR, files[1])))

    if args.simulate:
        bus = SimulatedD1()
    elif interface == AUTO:
        bus = AutoCanBus(cfg, transmit=True)
    else:
        bus = CanBus(interface)
        bus.open_tx()
    watcher.bus = bus
    start_thread('can-rx', rx_worker, bus, telemetry)
    start_thread('can-tx', tx_worker, bus, controller, tx_stats)
    if cfg['web'].getint('port'):
        start_web(cfg, WebApp(cfg, controller, telemetry, tx_stats))

    if dbus_service is None:
        while True:
            time.sleep(1)
            watcher(telemetry.snapshot())

    from gi.repository import GLib

    def tick():
        try:
            snap = telemetry.snapshot()
            watcher(snap)
            dbus_service.refresh(snap, controller.snapshot())
        except Exception:
            log.exception('D-Bus update failed')
        return True

    tick()
    GLib.timeout_add(int(cfg['dbus'].getfloat('update_interval') * 1000), tick)
    GLib.MainLoop().run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
