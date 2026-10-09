"""Codec for the Nomadic Innovations DC-DC converter CAN interface.

Source: NI-TLN-CONTROLS-DCDCCAN-R1 (2026-06).

Pure Python with no dependencies so it can be unit-tested on any PC.

All messages are Motorola (big endian). Start bits use the DBC convention: the
start bit is the MSB of the signal, numbered byte * 8 + bit, where bit 7 is the
MSB of each byte. Every signal in the spec packs contiguously under this
convention (e.g. DCDC_ELECTRIC_VALUE fills bits without gaps or overlaps),
which is how it was confirmed.
"""

ID_CTRL = 0x018        # EMS_DCDC_CTRL        controller -> DC/DC, 10 ms
ID_PARAMETER = 0x066   # DCDC_PARAMETER       DC/DC -> controller, 50 ms
ID_ELECTRIC = 0x088    # DCDC_ELECTRIC_VALUE  DC/DC -> controller, 10 ms
ID_VERSION = 0x520     # DCDC_VERSION         DC/DC -> controller, 100 ms

RX_IDS = (ID_PARAMETER, ID_ELECTRIC, ID_VERSION)

DIRECTION_OFF = 0
DIRECTION_BUCK = 1     # HV -> LV (48 V -> 12 V)
DIRECTION_BOOST = 2    # LV -> HV (12 V -> 48 V)

DIRECTION_NAMES = {
    DIRECTION_OFF: 'Off',
    DIRECTION_BUCK: 'Buck 48V -> 12V',
    DIRECTION_BOOST: 'Boost 12V -> 48V',
}

VOLTAGE_FACTOR = 0.015
VOLTAGE_MAX = 4094 * VOLTAGE_FACTOR   # 0xFFF is the error value -> 61.41 V
CURRENT_LIMIT_MAX = 254               # 0xFF is the error value
PRECHARGE_VOLTAGE_MIN = 24.0          # outside 24..54 V the spec calls it invalid
PRECHARGE_VOLTAGE_MAX = 54.0

# (start bit, length)
CTRL_DIRECTION = (1, 2)
CTRL_PRECHARGE = (4, 1)
CTRL_IMAX = (15, 8)
CTRL_UREG = (23, 12)
CTRL_UPRECH = (27, 12)

PAR_LOAD_STATUS = (1, 2)
PAR_FAULT_STATUS = (4, 3)
PAR_VOLTAGE_STATUS = (15, 8)
PAR_FAULT = (23, 10)
PAR_OPERATING_MODE = (27, 4)
PAR_PRECHARGE_STATUS = (29, 2)

EL_U14 = (7, 9)
EL_I14 = (14, 9)
EL_UHV = (21, 10)
EL_IHV = (27, 9)
EL_TEMP = (47, 8)

VER_SW = (7, 12)
VER_HW = (11, 4)

OPERATING_MODES = {
    0x1: 'Initialization in progress',
    0x2: 'Idle',
    0x3: 'Boost (12V -> 48V)',
    0x4: 'Buck (48V -> 12V)',
    0x5: 'Autonomous (default set-point)',
    0x7: 'Shut down in progress',
    0x8: 'Derating (over-temperature)',
    0xF: 'Invalid signal',
}

PRECHARGE_STATUSES = {
    0x0: 'Reserved',
    0x1: 'Precharge in process',
    0x2: 'Precharge not in process',
    0x3: 'Invalid signal',
}

LOAD_STATUS_BITS = (
    'Overcurrent on HV network',
    'Overcurrent on LV network',
)

FAULT_STATUS_BITS = (
    'Power limitation active (derating)',
    'Temporary failure',
    'Permanent failure',
)

VOLTAGE_STATUS_BITS = (
    'Undervoltage on LV network',
    'Low voltage on LV network',
    'High voltage on LV network',
    'Overvoltage on LV network',
    'Undervoltage on HV network',
    'Low voltage on HV network',
    'High voltage on HV network',
    'Overvoltage on HV network',
)

FAULT_BITS = (
    'Fault in T15170 chip',
    '5V abnormal',
    'Precharge target fault',
    'CAN loss',
    'Buck mode fault',
    'CVT fault',
    'Hardware fault',
    'Product overheating',
    'Power supply fault',
    'Sensor fault',
)


# Every alarm the converter can report as (decoded field, bit, text), most serious first.
ALARMS = (
    [('fault_status', 2, FAULT_STATUS_BITS[2]), ('fault_status', 1, FAULT_STATUS_BITS[1])]
    + [('faults', bit, text) for bit, text in enumerate(FAULT_BITS)]
    + [('load_status', bit, text) for bit, text in enumerate(LOAD_STATUS_BITS)]
    + [('voltage_status', bit, VOLTAGE_STATUS_BITS[bit]) for bit in (3, 7, 0, 4, 2, 6, 1, 5)]
    + [('fault_status', 0, FAULT_STATUS_BITS[0])]
)
FAULT_ALARM_COUNT = 14   # the first 14 ALARMS are faults; the rest are warnings


def _shift(signal):
    start, length = signal
    msb = (start // 8) * 8 + (7 - start % 8)   # position counted from MSB of byte 0
    return 64 - (msb + length)


def get_signal(data, signal):
    word = int.from_bytes(bytes(data).ljust(8, b'\0')[:8], 'big')
    return (word >> _shift(signal)) & ((1 << signal[1]) - 1)


def put_signal(word, signal, raw):
    mask = (1 << signal[1]) - 1
    if raw < 0 or raw > mask:
        raise ValueError('raw value %r does not fit in %d bits' % (raw, signal[1]))
    shift = _shift(signal)
    return (word & ~(mask << shift)) | (raw << shift)


def encode_ctrl(direction, precharge, current_limit, voltage_setpoint, precharge_voltage):
    """Build the 8-byte EMS_DCDC_CTRL (0x018) payload.

    Only checks what the frame can carry; operating limits belong to the caller.
    """
    if direction not in DIRECTION_NAMES:
        raise ValueError('direction must be 0, 1 or 2')
    if not 0 <= current_limit <= CURRENT_LIMIT_MAX:
        raise ValueError('current limit must be 0..%d A' % CURRENT_LIMIT_MAX)
    for v in (voltage_setpoint, precharge_voltage):
        if not 0 <= v <= VOLTAGE_MAX:
            raise ValueError('voltage must be 0..%.2f V' % VOLTAGE_MAX)

    word = 0
    word = put_signal(word, CTRL_DIRECTION, direction)
    word = put_signal(word, CTRL_PRECHARGE, 1 if precharge else 0)
    word = put_signal(word, CTRL_IMAX, int(round(current_limit)))
    word = put_signal(word, CTRL_UREG, int(round(voltage_setpoint / VOLTAGE_FACTOR)))
    word = put_signal(word, CTRL_UPRECH, int(round(precharge_voltage / VOLTAGE_FACTOR)))
    return word.to_bytes(8, 'big')


def decode_ctrl(data):
    """Decode a 0x018 frame (used by tests and for showing what is being sent)."""
    imax = get_signal(data, CTRL_IMAX)
    ureg = get_signal(data, CTRL_UREG)
    uprech = get_signal(data, CTRL_UPRECH)
    return {
        'direction': get_signal(data, CTRL_DIRECTION),
        'precharge': get_signal(data, CTRL_PRECHARGE),
        'current_limit': None if imax == 0xFF else imax,
        'voltage_setpoint': None if ureg == 0xFFF else round(ureg * VOLTAGE_FACTOR, 3),
        'precharge_voltage': None if uprech == 0xFFF else round(uprech * VOLTAGE_FACTOR, 3),
    }


def _encode(fields):
    word = 0
    for signal, raw in fields:
        word = put_signal(word, signal, raw)
    return word.to_bytes(8, 'big')


def encode_parameter(load_status=0, fault_status=0, voltage_status=0, faults=0,
                     operating_mode=0x2, precharge_status=0x2):
    """Build a DCDC_PARAMETER (0x066) payload, as the converter would send it (for simulation)."""
    return _encode(((PAR_LOAD_STATUS, load_status), (PAR_FAULT_STATUS, fault_status),
                    (PAR_VOLTAGE_STATUS, voltage_status), (PAR_FAULT, faults),
                    (PAR_OPERATING_MODE, operating_mode), (PAR_PRECHARGE_STATUS, precharge_status)))


def encode_electric(lv_voltage, lv_current, hv_voltage, hv_current, temperature):
    """Build a DCDC_ELECTRIC_VALUE (0x088) payload (for simulation). Values are clamped to range."""
    def clamp(v, hi):
        return max(0, min(hi, int(round(v))))
    return _encode(((EL_U14, clamp(lv_voltage * 10, 511)), (EL_I14, clamp(lv_current + 255, 511)),
                    (EL_UHV, clamp(hv_voltage * 10, 1023)), (EL_IHV, clamp(hv_current + 255, 511)),
                    (EL_TEMP, clamp(temperature + 48, 255))))


def encode_version(software_bcd, hardware):
    """Build a DCDC_VERSION (0x520) payload (for simulation)."""
    return _encode(((VER_SW, software_bcd), (VER_HW, hardware)))


def decode_parameter(data):
    """Decode DCDC_PARAMETER (0x066)."""
    mode = get_signal(data, PAR_OPERATING_MODE)
    prech = get_signal(data, PAR_PRECHARGE_STATUS)
    return {
        'load_status': get_signal(data, PAR_LOAD_STATUS),
        'fault_status': get_signal(data, PAR_FAULT_STATUS),
        'voltage_status': get_signal(data, PAR_VOLTAGE_STATUS),
        'faults': get_signal(data, PAR_FAULT),
        'operating_mode': mode,
        'operating_mode_text': OPERATING_MODES.get(mode, 'Reserved (0x%X)' % mode),
        'precharge_status': prech,
        'precharge_status_text': PRECHARGE_STATUSES[prech],
    }


def decode_electric(data):
    """Decode DCDC_ELECTRIC_VALUE (0x088). Volts, amps, degrees C."""
    return {
        'lv_voltage': round(get_signal(data, EL_U14) * 0.1, 1),
        'lv_current': get_signal(data, EL_I14) - 255,
        'hv_voltage': round(get_signal(data, EL_UHV) * 0.1, 1),
        'hv_current': get_signal(data, EL_IHV) - 255,
        'temperature': get_signal(data, EL_TEMP) - 48,
    }


def decode_version(data):
    """Decode DCDC_VERSION (0x520). Software version is BCD, one digit per nibble."""
    sw = get_signal(data, VER_SW)
    digits = ['%X' % ((sw >> s) & 0xF) for s in (8, 4, 0)]
    return {
        'software_raw': sw,
        'software': '.'.join(digits),
        'hardware': get_signal(data, VER_HW),
    }


def active_alarms(parameter):
    """Indexes into ALARMS of every active alarm in a decoded 0x066 frame, most serious first."""
    if parameter is None:
        return []
    return [i for i, (field, bit, _) in enumerate(ALARMS) if parameter[field] >> bit & 1]


def alarm_texts(parameter):
    """All active faults and warnings from a decoded 0x066 frame as text, most serious first."""
    return [ALARMS[i][2] for i in active_alarms(parameter)]
