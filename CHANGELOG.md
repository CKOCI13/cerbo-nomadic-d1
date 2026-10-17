# Changelog

What changed in each version of the driver (`VERSION` in `dbus-nomadic-dcdc/dbus-nomadic-dcdc.py`).

## Unreleased

- **Cerbo CAN port instructions:** the guide and README now cover both Cerbo models. On a **Cerbo GX MK2**, use
  **VE.Can 2** with "Up, but no services". On an **original Cerbo GX**, the BMS-Can port only runs at 500 kbit/s and
  can't be set to "Up, but no services", which the old step 3c asked for.
- **Install from a USB stick or SD card, with no network or SSH:** `Cerbo Setup.bat` option U (or
  `make-usb-installer.sh`) makes `venus-data.tar.gz`; Venus OS unpacks it at boot and its hook installs the service.
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
