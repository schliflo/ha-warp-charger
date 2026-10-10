"""Sensor platform tests."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.tinkerforge_warp.const import DOMAIN
from tests.common import (
    FIXTURES,
    Clock,
    check_availability_cycle,
    device_of,
    entity_id,
    frozen_time_fixture,
    get_state,
    merge,
    platform_fixture,
    push,
    setup_entry,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import REMOVE, FakeWarp

warp_platform = platform_fixture(Platform.SENSOR)

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


frozen_time = frozen_time_fixture()  # timestamp sensors depend on 'now'


def _state(hass: HomeAssistant, fake: FakeWarp, key: str) -> str:
    return get_state(hass, "sensor", fake, key).state


@pytest.mark.parametrize("fixture", FIXTURES)
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """All sensors of each device model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.mark.parametrize("fixture", FIXTURES)
async def test_devices(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """Main device and meter sub-devices."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    by_id = {d.id: d for d in devices}
    summary = [
        {
            "identifiers": sorted(d.identifiers),
            "name": d.name,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "model_id": d.model_id,
            "sw_version": d.sw_version,
            "hw_version": d.hw_version,
            "connections": sorted(d.connections),
            "via_device": sorted(by_id[d.via_device_id].identifiers) if d.via_device_id else None,
            "configuration_url": d.configuration_url == fake.base_url
            if not d.via_device_id
            else d.configuration_url,
        }
        for d in sorted(devices, key=lambda d: sorted(d.identifiers))
    ]
    assert summary == snapshot


async def test_charger_state(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """charger_state follows evse/state; unknown enum values become unknown."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charger_state") == "waiting_for_release"

    expected = ["not_connected", "waiting_for_release", "ready", "charging", "error"]
    for value, name in enumerate(expected):
        await merge(hass, fake, "evse/state", charger_state=value)
        assert _state(hass, fake, "charger_state") == name

    await merge(hass, fake, "evse/state", charger_state=9)
    assert _state(hass, fake, "charger_state") == STATE_UNKNOWN
    await merge(hass, fake, "evse/state", charger_state=3, allowed_charging_current=16000)
    assert _state(hass, fake, "charger_state") == "charging"
    assert _state(hass, fake, "allowed_charging_current") == "16.0"


async def test_error_and_iec_states(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Diagnostic enum sensors."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await merge(
        hass,
        fake,
        "evse/state",
        error_state=4,
        iec61851_state=4,
        dc_fault_current_state=5,
    )
    assert _state(hass, fake, "error_state") == "contactor_error"
    assert _state(hass, fake, "iec61851_state") == "e"
    assert _state(hass, fake, "dc_fault_state") == "ac"
    await merge(hass, fake, "evse/state", error_state=1)  # gap in the table
    assert _state(hass, fake, "error_state") == STATE_UNKNOWN


async def test_session_energy(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Session energy is energy_abs - meter_start; unknown without a session."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "session_energy") == "5.35"
    assert _state(hass, fake, "session_user") == "Anonymous"

    await merge(hass, fake, "meter/values", energy_abs=2200.0)
    assert _state(hass, fake, "session_energy") == "10.3"
    await merge(hass, fake, "meter/values", energy_abs=2000.0)  # meter reset -> clamped
    assert _state(hass, fake, "session_energy") == "0.0"

    await merge(hass, fake, "charge_tracker/current_charge", user_id=-1)
    for key in ("session_energy", "session_start", "session_user"):
        assert _state(hass, fake, key) == STATE_UNKNOWN

    await merge(hass, fake, "charge_tracker/current_charge", user_id=2)
    assert _state(hass, fake, "session_user") == "Benutzer 2"
    await merge(hass, fake, "charge_tracker/current_charge", user_id=99)
    assert _state(hass, fake, "session_user") == STATE_UNKNOWN


async def test_session_energy_from_meter_slot(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Without the legacy meter the charger's meter slot (value id 209) is used."""
    fake = await fake_warp("warp3", patches={"meter/values": REMOVE})
    await setup_entry(hass, fake)
    # meters/0 id 209 = 2195.05, meter_start = 2189.70
    assert _state(hass, fake, "session_energy") == "5.35"
    values = list(fake.states["meters/0/values"])
    values[fake.states["meters/0/value_ids"].index(209)] = 2190.7
    await push(hass, fake, "meters/0/values", values)
    assert _state(hass, fake, "session_energy") == "1.0"


async def test_last_session_energy(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The last finished session; unknown while there is none."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "last_session_energy") == "26.2"
    await push(hass, fake, "charge_tracker/last_charges", [])
    assert _state(hass, fake, "last_session_energy") == STATE_UNKNOWN


async def test_session_start(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, frozen_time: Clock
) -> None:
    """Start from timestamp_minutes, or from the EVSE uptime when the device has no RTC time."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    minutes = fake.states["charge_tracker/current_charge"]["timestamp_minutes"]
    assert (
        _state(hass, fake, "session_start") == datetime.fromtimestamp(minutes * 60, UTC).isoformat()
    )

    # No RTC time: uptime fallback, one hour ago, minute precision.
    uptime = fake.states["evse/low_level_state"]["uptime"]
    frozen_time.move_to(NOW + timedelta(seconds=30))
    await merge(
        hass,
        fake,
        "charge_tracker/current_charge",
        timestamp_minutes=0,
        evse_uptime_start=uptime - 3_600_000,
    )
    assert _state(hass, fake, "session_start") == "2026-10-10T11:00:00+00:00"

    # The 32-bit EVSE uptime wrapped since the session started.
    await merge(hass, fake, "evse/low_level_state", uptime=1000)
    await merge(
        hass,
        fake,
        "charge_tracker/current_charge",
        timestamp_minutes=0,
        evse_uptime_start=2**32 - 599_000,
    )
    assert _state(hass, fake, "session_start") == "2026-10-10T11:50:00+00:00"


@pytest.mark.parametrize(
    ("fixture", "key", "domain_device"),
    [("warp3", "meter_0_power_active_l_sum_im_ex_diff", "main")],
)
async def test_charger_meter_on_main_device(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    fixture: str,
    key: str,
    domain_device: str,
) -> None:
    """The charger's own meter slot lives on the main device."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    device = device_of(hass, "sensor", fake, key)
    assert device.identifiers == {(DOMAIN, "2jEx")}
    assert device.id == entry.runtime_data.main_device_id
    assert device_of(hass, "sensor", fake, "charger_state").id == device.id


async def test_meter_values(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Meter values are rounded to the firmware's digits; null is unknown."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    ids = fake.states["meters/0/value_ids"]
    values = list(fake.states["meters/0/values"])
    values[ids.index(1)] = 231.26789  # V, 1 digit
    values[ids.index(14)] = 1.23456  # A, 3 digits
    values[ids.index(74)] = 1234.567  # W, 0 digits
    values[ids.index(209)] = 2196.123456  # kWh, 3 digits
    values[ids.index(364)] = None
    await push(hass, fake, "meters/0/values", values)
    assert _state(hass, fake, "meter_0_voltage_l1_n") == "231.3"
    assert _state(hass, fake, "meter_0_current_l1_im_ex_diff") == "1.235"
    assert _state(hass, fake, "meter_0_power_active_l_sum_im_ex_diff") == "1235"
    assert _state(hass, fake, "meter_0_energy_active_l_sum_import") == "2196.123"
    assert _state(hass, fake, "meter_0_frequency_l_avg") == STATE_UNKNOWN


async def test_meters_on_sub_devices(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Without an EVSE every meter slot is a sub-device named after its display_name."""
    fake = await fake_warp("wem2")
    entry = await setup_entry(hass, fake)
    main = entry.runtime_data.main_device_id
    registry = dr.async_get(hass)
    expected = {
        "meter_0_power_active_l_sum_im_ex_diff": "WARP Energy Manager",
        "meter_1_power_active_l_sum_im_ex_diff": "Example Inverter",
        "meter_2_power_active_l_sum_im_ex_diff": "Example Battery",
    }
    for key, name in expected.items():
        device = device_of(hass, "sensor", fake, key)
        assert device.name == name
        assert device.via_device_id == main
        assert device.id != main
    assert _state(hass, fake, "meter_1_power_active_l_sum_im_ex_diff") == "-3200"
    assert _state(hass, fake, "meter_2_state_of_charge") == "62.5"

    # Diagnostic and energy-management sensors stay on the main device.
    for key in ("last_restart", "electricity_price"):
        assert device_of(hass, "sensor", fake, key).id == main
    assert registry.async_get(main).name == "wem2-energy-manager"

    # A meter without display_name falls back to its slot number.
    config = fake.states["meters/2/config"]
    await push(hass, fake, "meters/2/config", [config[0], {**config[1], "display_name": ""}])
    assert entry.runtime_data.device_info("meter_2")["name"] == "Meter 2"


async def test_firmware_drift(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A payload missing a key makes the value unknown without any error being logged."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    caplog.clear()
    caplog.set_level(logging.WARNING)

    state = dict(fake.states["evse/state"])
    del state["charger_state"], state["allowed_charging_current"]
    await push(hass, fake, "evse/state", state)
    assert _state(hass, fake, "charger_state") == STATE_UNKNOWN
    assert _state(hass, fake, "allowed_charging_current") == STATE_UNKNOWN

    await push(hass, fake, "evse/low_level_state", {"uptime": 1})
    assert _state(hass, fake, "temperature") == STATE_UNKNOWN
    assert _state(hass, fake, "phases_active") == STATE_UNKNOWN

    await push(hass, fake, "esp32/temperature", {})
    assert _state(hass, fake, "esp_temperature") == STATE_UNKNOWN

    await push(hass, fake, "charge_tracker/current_charge", {"user_id": 0})
    assert _state(hass, fake, "session_energy") == STATE_UNKNOWN
    assert _state(hass, fake, "session_start") == STATE_UNKNOWN

    await push(hass, fake, "meters/0/values", [])
    assert _state(hass, fake, "meter_0_power_active_l_sum_im_ex_diff") == STATE_UNKNOWN

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    # Recovery.
    await push(hass, fake, "evse/state", {**state, "charger_state": 2})
    assert _state(hass, fake, "charger_state") == "ready"


async def test_temperature_and_phases(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Temperature in 0.1 degC; phases_active is unknown while no phases are in use."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "temperature") == "31.9"
    assert _state(hass, fake, "phases_active") == "1"
    await merge(hass, fake, "evse/low_level_state", temperature=2544, phases_current=3)
    assert _state(hass, fake, "temperature") == "25.4"
    assert _state(hass, fake, "phases_active") == "3"
    await merge(hass, fake, "evse/low_level_state", phases_current=0)
    assert _state(hass, fake, "phases_active") == STATE_UNKNOWN


async def test_write_on_change(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Hot topics only write state when the derived value changed."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    eid = entity_id(hass, "sensor", fake, "temperature")
    assert eid is not None
    before = hass.states.get(eid)
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)
    reported_at = before.last_reported

    def for_entity(events: list[Event]) -> list[Event]:
        return [e for e in events if e.data["entity_id"] == eid]

    # Irrelevant fields (1 Hz noise) and an identical re-send: no write at all.
    low = fake.states["evse/low_level_state"]
    await push(hass, fake, "evse/low_level_state", {**low, "time_since_state_change": 1})
    await push(hass, fake, "evse/low_level_state", {**low, "adc_values": [1, 2, 3]})
    await push(hass, fake, "evse/low_level_state", dict(low))
    assert for_entity(changed) == []
    assert hass.states.get(eid).last_reported == reported_at  # not even a 'reported' write
    assert hass.states.get(eid) is before

    # Sub-resolution change (0.01 degC) rounds to the same 0.1 degC value.
    await push(hass, fake, "evse/low_level_state", {**low, "temperature": low["temperature"] + 3})
    assert for_entity(changed) == []
    assert hass.states.get(eid) is before

    # A real change is written once.
    await push(hass, fake, "evse/low_level_state", {**low, "temperature": 3500})
    assert len(for_entity(changed)) == 1
    assert hass.states.get(eid).state == "35.0"


async def test_esp_temperature(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """ESP temperature in degC."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "esp_temperature") == "34.8"
    await push(hass, fake, "esp32/temperature", {"temperature": 4012})
    assert _state(hass, fake, "esp_temperature") == "40.1"


def _iso(value: datetime) -> str:
    """State representation of a timestamp (whole seconds)."""
    return value.replace(microsecond=0).isoformat()


async def test_last_restart(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, frozen_time: Clock
) -> None:
    """The keep-alive uptime yields the restart time; jitter of 5 s or less is ignored."""
    fake = await fake_warp("wem2")
    await setup_entry(hass, fake)
    uptime = fake.states["info/keep_alive"]["uptime"]
    assert _state(hass, fake, "last_restart") == _iso(NOW - timedelta(milliseconds=uptime))
    eid = entity_id(hass, "sensor", fake, "last_restart")
    assert eid is not None
    before = hass.states.get(eid)

    # The device keeps counting while time passes: the restart time is stable.
    frozen_time.tick(timedelta(seconds=2))
    later = NOW + timedelta(seconds=2)
    await push(hass, fake, "info/keep_alive", {"uptime": uptime + 2000})
    assert hass.states.get(eid) is before

    # Jitter of 3 s is ignored ...
    await push(hass, fake, "info/keep_alive", {"uptime": uptime + 5000})
    assert hass.states.get(eid) is before
    # ... more than 5 s (measured against the last written value) is not.
    await push(hass, fake, "info/keep_alive", {"uptime": uptime + 8100})
    assert hass.states.get(eid).state == _iso(later - timedelta(milliseconds=uptime + 8100))

    # A reboot (uptime reset) is a real change.
    await push(hass, fake, "info/keep_alive", {"uptime": 1000})
    assert hass.states.get(eid).state == _iso(later - timedelta(seconds=1))


async def test_energy_management_sensors(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Price, forecast and §14a limit including their 'no data' sentinels."""
    fake = await fake_warp("wem2")
    await setup_entry(hass, fake)
    price: dict[str, Any] = fake.states["day_ahead_prices/state"]
    assert _state(hass, fake, "electricity_price") == "25.123"
    await push(hass, fake, "day_ahead_prices/state", {**price, "current_price": 2**31 - 1})
    assert _state(hass, fake, "electricity_price") == STATE_UNKNOWN
    await push(hass, fake, "day_ahead_prices/state", {**price, "current_price": -1500})
    assert _state(hass, fake, "electricity_price") == "-1.5"

    forecast = fake.states["solar_forecast/state"]
    await push(
        hass, fake, "solar_forecast/state", {**forecast, "wh_today": -1, "wh_tomorrow": 8500}
    )
    assert _state(hass, fake, "solar_forecast_today") == STATE_UNKNOWN
    assert _state(hass, fake, "solar_forecast_tomorrow") == "8.5"

    assert _state(hass, fake, "p14a_limit") == STATE_UNKNOWN
    p14a = fake.states["p14a_enwg/state"]
    await push(hass, fake, "p14a_enwg/state", {**p14a, "active": True, "limit_w": 4200})
    assert _state(hass, fake, "p14a_limit") == "4200"


async def test_vehicle_sensors(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Vehicle name and capacity from ev/state (WARP4)."""
    fake = await fake_warp("warp4")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "vehicle_name") == "Test EV"
    assert _state(hass, fake, "vehicle_capacity") == "64.0"
    await merge(hass, fake, "ev/state", name="", capacity=77.0)
    assert _state(hass, fake, "vehicle_name") == STATE_UNKNOWN
    assert _state(hass, fake, "vehicle_capacity") == "77.0"


async def test_meter_without_charger_slot(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """If the charger's meter slot is unknown, meter 0 is a regular sub-device."""
    fake = await fake_warp("warp3", patches={"evse/meter_config": REMOVE})
    entry = await setup_entry(hass, fake)
    device = device_of(hass, "sensor", fake, "meter_0_power_active_l_sum_im_ex_diff")
    assert device.identifiers == {(DOMAIN, "2jEx_meter_0")}
    assert device.via_device_id == entry.runtime_data.main_device_id
    assert _state(hass, fake, "session_energy") == "5.35"


async def test_no_optional_sensors_without_config(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """No energy-management config and no ESP temperature: no such sensors."""
    fake = await fake_warp(
        "warp3",
        patches={
            "p14a_enwg/config": REMOVE,
            "solar_forecast/config": REMOVE,
            "day_ahead_prices/config": REMOVE,
            "esp32/temperature": REMOVE,
        },
    )
    await setup_entry(hass, fake)
    for key in ("p14a_limit", "electricity_price", "solar_forecast_today", "esp_temperature"):
        assert entity_id(hass, "sensor", fake, key) is None
    assert entity_id(hass, "sensor", fake, "charger_state") is not None


async def test_wifi_rssi(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The Wi-Fi sensor exists only with an enabled station and hides the no-signal value."""
    fake = await fake_warp("warp3", patches={"wifi/sta_config": {"enable_sta": True}})
    await setup_entry(hass, fake)
    assert _state(hass, fake, "wifi_rssi") == STATE_UNKNOWN  # -127: not connected
    await merge(hass, fake, "wifi/state", sta_rssi=-61)
    assert _state(hass, fake, "wifi_rssi") == "-61"
    await merge(hass, fake, "wifi/state", sta_rssi=0)
    assert _state(hass, fake, "wifi_rssi") == STATE_UNKNOWN


async def test_no_wifi_sensor_on_ethernet(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Wired devices get no Wi-Fi sensor."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert entity_id(hass, "sensor", fake, "wifi_rssi") is None


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Sensors go unavailable after the grace period and recover on reconnect."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    ids = [
        entity_id(hass, "sensor", fake, key) or ""
        for key in ("charger_state", "session_energy", "meter_0_power_active_l_sum_im_ex_diff")
    ]
    await check_availability_cycle(hass, fake, ids)
    assert _state(hass, fake, "charger_state") == "waiting_for_release"
