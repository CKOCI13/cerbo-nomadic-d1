# Changelog

What changed in each version of the driver (`VERSION` in `dbus-nomadic-dcdc/dbus-nomadic-dcdc.py`).

## 1.5.1 (2026-10-31)

- **Fahrenheit.** The web page now shows the converter temperature in the unit set on the Cerbo (**Settings → General → Display & Appearance → Data units → Temperature**), the same unit the Cerbo screen and VRM already used for it. Choose Fahrenheit there for °F. The live reading, the temperature chart and the history table all follow it, and a change on the Cerbo shows on the page within about 10 seconds. To force one unit on the page, set `temperature_unit = fahrenheit` or `celsius` in the `[web]` part of `config.ini`. The default is `auto`. On a PC (`--simulate` or `--no-dbus`) the page shows °F.
- **Docs in °F first:** the Safe Test Plan's limits (248 °F / 120 °C shutdown, about 176 °F / 80 °C to stop a test), the worked example in "How it works" and the Cerbo pictures now give Fahrenheit. The installation guide and the driver reference say where to choose the unit.
- New tests: `tests/test_temperature_unit.py`.

## Documentation update 2 (2026-10-25, driver unchanged)

- **Plain-language pass** for the README, Installation Guide and Safe Test Plan: long sentences split up, the troubleshooting checklist made into a numbered list, and new glossary entries (lever connector, current limit, Automatic with limits).
- **New test, `tests/test_spec_conformance.py`:** checks the driver independently against Nomadic's CAN interface document, the Rev D manual and Victron's USB install script.

## Documentation update (2026-10-25, driver unchanged)

- **Cable: no CAN ground wire.** The Cerbo and the D1 already share the van's negative, and the Cerbo's VE.Can 2 port isn't isolated, so the white/green wire (RJ45 pin 3) is now left unused and insulated. The guide adds a one-time check of the voltage between the Cerbo's negative and the D1's GND.
- **Joining with lever connectors.** The D1's signal plug already has labelled CAN_H and CAN_L wires, so the guide now joins them to the network cable's brown pair with two 3-way lever wire connectors (e.g. WAGO 221-413), with the 120 Ω resistor between the connectors. No crimping.
- **New wiring pictures** from one shared source (`docs/src/wiring.html`, rebuilt with `node docs/src/build-wiring.cjs`), including a close-up of the two lever connectors and the resistor. Installation Guide, Safe Test Plan, driver reference, README and "How it works" updated.

## 1.5.0 (2026-10-25)

- **Automatic with limits** (new default, `automatic = limited`): in Automatic the Cerbo now chooses buck, boost or standby itself, by the D1 manual's own rules (buck below 12.5 V while the 48 V side is 41–58 V, standby to 13.5 V, boost from 13.5 V until 12.3 V), and sends the configured current limits every 10 ms, so owners never pick Buck or Boost and the 12 V cable stays within its limit. Each change waits 2 s (an engine-crank dip is ignored), boost waits 60 s after a low-voltage stop (no buck/boost flapping on a weak alternator), it never asks for buck and boost together or goes straight from one to the other, and it sends Off if the readings stop. The web page and `/Nomadic/Control/AutoReason` say what it chose and why. `automatic = hands_off` keeps the old behaviour (send nothing). New tests (`tests/test_auto_logic.py`) include a 20-minute closed loop against the pretend D1: at most 100 A on the 12 V side, against 263 A with the D1's own logic.
- **Pretend D1:** now has an engine that starts and stops every 3 minutes, and boosts at its factory limits in hands-off mode.
- **Safe Test Plan:** stages 3–5 now set `automatic = hands_off`; a new stage 6 tests Automatic with limits. The D1's test plan now has an HTML source and is rebuilt by `docs/src/build.cjs`.
- **CAN port profile you can actually choose:** the guide said to set the converter's port to *Up, but no services*. Victron's current screen software (gui-v2, Venus OS v3.80+) hides that option and won't let you pick it. The guide, the driver reference and the `--scan` advice now say **VE.Can (250 kbit/s)** or **CAN-bus BMS LV (500 kbit/s)**; the driver reference shows how to set *Up, but no services* over SSH if you want it.
- **Start-up script fix (USB stick and normal installs):** Venus OS only runs `/data/rc.local` when it is executable, and when another add-on ends that file with `exit 0`, a line added after it never runs. `install.sh` now puts its line before the first top-level `exit`, moves it there if an older version left it after one, and always makes the file executable. New tests cover it, and the USB file was checked against Victron's own `update-data.sh`.
- **USB file names:** the README and guide now warn that a browser-renamed file such as `... (1).tar.gz` is skipped by the Cerbo (names with spaces don't work) and must be renamed back.
- **Boost current limit explained:** `boost_current_limit` is counted on the 48 V side, so the 12 V cable and breaker carry up to about 5 times as many amps (30 A boost is about 154 A on the 12 V side at worst). The README, `config.ini` and the guide now give the conversion and a worked example for a 150 A breaker.
- **D1 manual Rev D:** references updated from Rev C. The guide now gives Rev D's fuses: 3 A for KL30 and 1 A for KL15.
- **Current limits preset for a stock Grand Design LTV1** (2 AWG cable on a 150 A fuse, kept to 120 A): `buck_current_limit_max = 120`, `boost_current_limit_max = 23` (about 118 A on the 12 V side at worst), and Boost now starts at 20 A. Raise them only if your cable and fuse are bigger.
- **KL15 joined to KL30** (as on the LTV1): the safety notes and troubleshooting now say to pull that wire's fuse, since there is no KL15 switch.

## 1.4.0 (2026-10-17): test release

- **Finds its CAN port by itself:** `interface = auto` is the new default. The driver listens on every CAN
  port and uses the one it hears the D1 on, so the same file works on any Cerbo (`can1` on the original,
  `vecan1` on the MK2) with no settings to edit. Nothing is sent before the converter has been heard, and only on its port. The device page shows the port it found.
- **USB stick / SD card installs once:** the file is now `venus-data-nomadic-d1.tar.gz`, and after installing, the
  Cerbo renames it on the stick to `...tar.gz.installed`, so leaving the stick in can't reinstall it or put old
  settings back. A ready-made file is attached to each release, so you no longer need to build it.
- **Venus OS "Modification checks":** the guide now says to check that modifications are enabled (the default);
  when they are off, Venus OS disables the start-up script and the driver disappears after a restart.
- **Cerbo CAN port instructions:** the guide and README now cover both Cerbo models. On a **Cerbo GX MK2**, use
  **VE.Can 2** with "Up, but no services". On an **original Cerbo GX**, the BMS-Can port only runs at 500 kbit/s and
  can't be set to "Up, but no services", which the old step 3c asked for.
- **Install from a USB stick or SD card, with no network or SSH:** `Cerbo Setup.bat` option U (or
  `make-usb-installer.sh`) makes the installer file; Venus OS unpacks it at boot and its hook installs the service.
- This repository is now the **control edition, for advanced users.** A separate **monitor-only edition**, which
  never sends the D1 anything, is at [cerbo-nomadic-d1-monitor](https://github.com/CKOCI13/cerbo-nomadic-d1-monitor).
- New README section: **limiting current to protect fuses and wiring**, and why that limit is not a replacement for
  correctly sized fuses and cables.

## 1.3.0 (2026-10-06): safe testing

- **Simulation mode** (`--simulate`, Cerbo Setup option 9). The real driver runs against a pretend D1 and never
  opens a CAN socket. It registers separately, so it can run next to a real installation.
- **Test timer** (`test_timer` in `config.ini`): Buck or Boost switches itself to Off after a set number of minutes.
- **"Find the converter"** now shows each port's bitrate and CAN state, and spots a D1 talking at the other
  speed from the bus error count.
- **Buck target default 13.5 V,** the usual target for charging chassis batteries.
- **Safe Test Plan** document.

## 1.2.0 (2026-10-05): matched to the D1 manual

- **Automatic mode is the default.** The driver sends nothing and the D1 runs on its own logic, exactly as without
  a Cerbo. Off, Buck and Boost take control only when chosen.
- **Separate buck and boost current limits,** because the D1's limits differ by direction (178 A / 57 A).
- **Limits match the D1 manual:** buck target 10–13.5 V, boost target 42–57.8 V.
- **Cable instructions match the D1's 6-pin signal connector:** CAN-H on pin 1, CAN-L on pin 2, KL15 / KL30
  power-up wires, CAN ground to the common ground, and a 120 Ω terminator at the D1.
- **`--scan`** finds which Cerbo port the converter is on.
- **`Cerbo Setup.bat`,** a menu-driven Windows helper that copies, tests and installs the driver.
- Precharge controls are hidden by default. Short status labels fit the GX tiles.

## 1.1.0 (2026-10-05): Victron screen and VRM

- **Readings and controls in the GX switch pane, device page, VRM portal and VRM app,** via Victron's switch API.
- Names, cards and visibility changed on the GX are remembered.
- Alarms are sorted most serious first.

## 1.0.0 (2026-10-05): first version

- Decodes the D1's CAN messages and publishes them on D-Bus as a DC-DC converter.
- Sends the control message every 10 ms, with limits on every value and a pause before reversing direction.
- Built-in web page with a JSON API and an optional password.
- `--monitor` listen-only tool. Installs as a Venus OS service that survives firmware updates.
