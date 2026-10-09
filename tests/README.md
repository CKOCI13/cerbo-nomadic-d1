# Tests

Automated tests for the driver. They run on any computer with Python 3: no Cerbo, no converter and no extra
packages needed.

## Running them

From the project folder (the one with `README.md` in it):

```bash
python -m unittest discover -s tests -v
```

On Mac or Linux, type `python3` instead of `python`. They take a few seconds. GitHub also runs them
automatically on every change; the result is the "tests" badge at the top of the [project README](../README.md).

## What each file checks

| File | What it proves |
|---|---|
| [`test_codec.py`](test_codec.py) | Encoding and decoding of the four CAN messages, checked against frames worked out **by hand** from Nomadic's specification. The alarm list covers every status bit exactly once |
| [`test_controller.py`](test_controller.py) | Starts in Automatic and sends nothing. Buck and Boost use their own set-points and current limits. Reversing always passes through Off. Values outside the limits are refused (all of a change, or none of it). Settings survive a restart. The test timer switches Buck off |
| [`test_telemetry_and_web.py`](test_telemetry_and_web.py) | Readings go stale when the D1 goes quiet. Short frames are ignored. The web page needs its password, refuses posts from other web sites, and checks every value |
| [`test_dbus.py`](test_dbus.py) | Everything shown on the Cerbo and in VRM meets the display rules in Victron's GUI source code. Readings, alarms, "No communication", writes from the screen or VRM (and refusals), the on/off switch, mirroring changes made elsewhere, renaming that survives a restart, turning the switch pane off |
| [`test_scan.py`](test_scan.py) | "Find the converter" finds the right port, spots a wrong bitrate from bus errors, warns about old firmware, and works without `iproute2` |
| [`test_simulation.py`](test_simulation.py) | The pretend D1 follows the manual's automatic logic, obeys Off / Buck / Boost, protects its 12 V side, and flags CAN loss. The whole driver runs end to end against it |

## How they run without a Cerbo

- [`support.py`](support.py) loads the driver by its file path (its name contains dashes, so a normal `import`
  doesn't work) and puts the driver folder on the import path.
- [`fakes/vedbus.py`](fakes/vedbus.py) stands in for Victron's `vedbus` library, which needs the Linux D-Bus.
  It keeps the same rules: paths are added before registering, and outside writes go through the driver's
  checks with velib's return codes.
- Nothing here opens a real CAN socket. The scan tests use a temporary folder that looks like `/sys/class/net`,
  and the simulation tests use the pretend D1.
