# Tinkerforge WARP for Home Assistant

Local, broker-free integration for [Tinkerforge WARP](https://www.warp-charger.com/) wallboxes. It keeps one WebSocket open to the charger for push updates and sends commands over HTTP. No MQTT broker, no cloud, no polling.

## Supported devices

| Device | Status |
|---|---|
| WARP3 Charger (Smart/Pro) | Tested against real hardware (firmware 2.12) |
| WARP2 Charger | Expected to work, tested against synthetic data |
| WARP4 Charger | Expected to work, incl. vehicle data; tested against synthetic data |
| WARP Energy Manager (1.0/2.0) | Meters, charge mode, prices, forecast, §14a; no relays/inputs yet |

Entities appear only when the device supports and enables the feature (e.g. phase switching, NFC, charge manager, §14a EnWG).

## Installation

1. HACS → *Integrations* → ⋮ → *Custom repositories* → add this repository as *Integration*.
2. Install **Tinkerforge WARP**, restart Home Assistant.
3. The charger is discovered automatically (zeroconf). Otherwise: *Settings → Devices & services → Add integration → Tinkerforge WARP*.

### Configuration parameters

| Parameter | Description |
|---|---|
| Host | Hostname or IP, optionally with port (`warp3-abc.local`, `192.168.1.20:8080`). |
| Username / password | Only if the web interface has a login (*System → Users*). |
| Use HTTPS | Connect via HTTPS. Recommended with a password (see limitations). |
| Verify SSL certificate | Leave off for the charger's self-signed certificate. |

Host and credentials can be changed later via *Reconfigure*.

## What you get

- **Charger:** charger state, allowed current, error/IEC 61851/DC fault state, active phases, temperature; binary sensors plugged/charging/problem.
- **Control:** charging current limit (0 = pause, 6–32 A), start/stop charging, auto start, boost mode, phase auto switch, 1p/3p phase switching, charge mode (PV, Min + PV, Eco, …) when the charge manager or load management is active.
- **Session:** session energy, start, user, last session energy; charge limits (duration, energy, SoC on WARP4).
- **Meters:** power, energy import/export (Energy dashboard ready), optional per-phase voltages, currents, power, frequency; additional meters (grid, PV, battery) as sub-devices.
- **NFC:** `event` entity per scanned tag; tags also show up in HA's *Tags*.
- **Vehicle (WARP4):** SoC (writable: feed it from your car integration for SoC limits), capacity, name.
- **Energy management:** §14a EnWG limit, day-ahead price, solar forecast.
- **Maintenance:** firmware update entity (checks daily, installs from HA), reboot button, diagnostics download.

## Dashboard card

The integration ships a **WARP Charger** card (loaded automatically, no resource to add): charger state steps as in the web interface, power with L1/L2/L3 phase indicators, session energy/duration/NFC user, charge limit progress, vehicle SoC, current limit, charge mode, 1p/3p, boost and start/stop. Add it via *Edit dashboard → Add card → WARP Charger*, or:

```yaml
type: custom:warp-charger-card
device: <device_id>      # picked in the card editor
name: Garage             # optional
show_controls: true      # optional, false = status only
```

Controls appear only for entities that exist and are enabled.

## How data is updated

The charger pushes every state change over its WebSocket (at most once per second per topic); entities only write a new state when their value changes. Commands are sent via HTTP and the resulting state arrives through the push channel. If the connection drops, entities stay available for 30 s (rides out reboots), then become unavailable until the connection is back.

## Examples

Pause charging when the house battery is low:

```yaml
automation:
  - trigger:
      - platform: numeric_state
        entity_id: sensor.home_battery_soc
        below: 20
    action:
      - action: number.set_value
        target:
          entity_id: number.warp3_charger_pro_charging_current_limit
        data:
          value: 0
```

Feed the car's SoC to a WARP4 for SoC charge limits:

```yaml
automation:
  - trigger:
      - platform: state
        entity_id: sensor.my_car_battery_level
    action:
      - action: number.set_value
        target:
          entity_id: number.warp4_charger_pro_vehicle_soc
        data:
          value: "{{ states('sensor.my_car_battery_level') | float }}"
```

## Known limitations

- **Shared current slot:** the charging current limit writes the charger's *external* current slot. evcc and other tools write the same slot; the last writer wins.
- **Start/stop without a car** is silently ignored by the charger.
- **Charge limit overrides** apply to the current (or next) session only, then revert to the defaults from the web interface.
- **Firmware install** is refused by the charger while a vehicle is connected.
- **5 WebSocket clients max.** Each open web interface tab, evcc instance, etc. uses one. If Home Assistant keeps getting dropped, a repair issue appears.
- **Digest auth over HTTP** does not protect against replay on the local network; use HTTPS when a password is set.
- Not managed from HA: users, NFC tag assignments, automation rules, OCPP, network and charge-manager configuration (use the web interface).

## Troubleshooting

- **Duplicate entities:** turn off the charger's own MQTT Home Assistant auto discovery (MQTT settings in the web interface).
- **Entities unavailable:** check that the charger's web interface is reachable from the HA host; download *Diagnostics* from the device page and attach it to an issue.
- **Missing entities:** they only appear when the feature is enabled on the charger; after changing the configuration on the charger the integration reloads automatically (within ~15 s).

## Removal

*Settings → Devices & services → Tinkerforge WARP → ⋮ → Delete*, then remove it in HACS. Nothing is changed on the charger.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run python lib/scripts/record_dump.py <host> --replace-uid -o tests/fixtures/<model>.jsonl
```

The client library [`warp-charger`](https://github.com/schliflo/warp-charger) is an external dependency from PyPI, pinned in `pyproject.toml` and `manifest.json` (a test keeps both in sync). To test against an unreleased local checkout:

```sh
uv sync
uv pip install -e ../warp-charger
uv run --no-sync pytest
```
