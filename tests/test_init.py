"""Setup, unload, availability, structure watch and base entity tests."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockPlatform,
    async_fire_time_changed,
    mock_platform,
)

from custom_components.tinkerforge_warp import READY_TIMEOUT
from custom_components.tinkerforge_warp.const import (
    CONF_URL,
    DOMAIN,
    GRACE_PERIOD,
    RELOAD_DEBOUNCE,
)
from custom_components.tinkerforge_warp.entity import (
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    WarpRuntime,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import REMOVE, FakeWarp
from warp_charger import WarpClient

AUTH = ("admin", "secret")


@pytest.fixture(autouse=True)
def fast_client() -> Generator[None]:
    """Quick reconnects; no receive timeout (HA's time travel would trip it)."""
    with patch(
        "custom_components.tinkerforge_warp.WarpClient",
        partial(
            WarpClient,
            reconnect_delay=0.05,
            max_reconnect_delay=0.05,
            going_away_delay=0.05,
            jitter=0,
            receive_timeout=1000,
        ),
    ):
        yield


def _entry(fake: FakeWarp, **data: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="2jEx",
        title="warp3-charger-pro",
        data={CONF_URL: fake.base_url, CONF_VERIFY_SSL: False, **data},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _wait_for(predicate: Callable[[], bool], limit: float = 5) -> None:
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def _fire(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _runtime(entry: MockConfigEntry) -> WarpRuntime:
    return entry.runtime_data


async def test_setup_and_unload(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Setup registers the main device; unload closes the WebSocket."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _runtime(entry).uid == "2jEx"

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "2jEx"), entry.entry_id)
    assert device is not None
    assert device.name == "warp3-charger-pro"
    assert device.manufacturer == "Tinkerforge"
    assert device.model == "WARP3 Charger Pro 11kW"
    assert device.model_id == "warp3"
    assert device.sw_version == "2.12.1+6a4f9137"
    assert device.hw_version == "EVSE 3.0"
    assert device.connections == {("mac", "00:00:00:00:00:00")}
    assert device.configuration_url == fake.base_url

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    await _wait_for(lambda: fake.client_count == 0)


async def test_setup_with_credentials(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Digest credentials are used for the WebSocket."""
    fake = await fake_warp("warp3", auth=AUTH)
    entry = _entry(fake, **{CONF_USERNAME: "admin", CONF_PASSWORD: "secret"})
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_wifi_mac_fallback(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Without ethernet the Wi-Fi station MAC identifies the device."""
    fake = await fake_warp(
        "warp3",
        patches={
            "ethernet/state": {"mac": ""},
            "wifi/state": {"sta_mac": "AA:BB:CC:DD:EE:FF"},
        },
    )
    entry = _entry(fake)
    await _setup(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "2jEx"), entry.entry_id)
    assert device is not None
    assert device.connections == {("mac", "aa:bb:cc:dd:ee:ff")}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_device_without_optional_topics(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Missing hardware/display-name topics only drop the dependent device fields."""
    fake = await fake_warp(
        "warp3", patches={"evse/hardware_configuration": REMOVE, "info/display_name": REMOVE}
    )
    entry = _entry(fake)
    await _setup(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "2jEx"), entry.entry_id)
    assert device is not None
    assert device.name == "warp3-2jEx"
    assert device.hw_version is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("mode", ["hold_dump", "unreachable"])
async def test_not_ready(hass: HomeAssistant, fake_warp: FakeWarpFactory, mode: str) -> None:
    """No complete dump in time -> setup retry."""
    fake = await fake_warp("warp3", hold_dump=mode == "hold_dump")
    entry = _entry(fake, **({CONF_URL: "http://127.0.0.1:1"} if mode == "unreachable" else {}))
    with patch("custom_components.tinkerforge_warp.READY_TIMEOUT", 0.3):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert READY_TIMEOUT == 30
    await hass.config_entries.async_unload(entry.entry_id)
    await _wait_for(lambda: fake.client_count == 0)


async def test_auth_failed(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A 401 during setup fails the entry and starts reauth."""
    fake = await fake_warp("warp3", auth=AUTH)
    entry = _entry(fake)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_auth_lost_at_runtime(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Changed credentials on reconnect start a reauth flow."""
    fake = await fake_warp("warp3", auth=AUTH)
    entry = _entry(fake, **{CONF_USERNAME: "admin", CONF_PASSWORD: "secret"})
    await _setup(hass, entry)
    fake.auth = ("admin", "changed")
    await fake.drop_clients(1006)
    await _wait_for(lambda: bool(hass.config_entries.flow.async_progress_by_handler(DOMAIN)))
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["source"] == "reauth"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_grace_period(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A disconnect is hidden for 30 s, then reported, and cleared on reconnect."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    runtime = _runtime(entry)
    changes: list[bool] = []
    runtime.subscribe_available(lambda: changes.append(runtime.available))

    fake.hold_dump = True  # reconnects never complete the dump
    await fake.drop_clients(1006)
    await _wait_for(lambda: not runtime.client.connected)
    assert runtime.available

    await _fire(hass, GRACE_PERIOD - 1)
    assert runtime.available
    await _fire(hass, GRACE_PERIOD + 1)
    assert not runtime.available
    assert changes == [False]

    fake.hold_dump = False
    await fake.drop_clients(1006)
    await _wait_for(lambda: runtime.available)
    assert changes == [False, True]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconnect_within_grace_period(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A quick reconnect (reboot) never shows as unavailable."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    runtime = _runtime(entry)
    changes: list[bool] = []
    runtime.subscribe_available(lambda: changes.append(runtime.available))

    await fake.reboot()
    await _wait_for(lambda: fake.ws_connect_count == 2 and runtime.client.connected)
    await _fire(hass, GRACE_PERIOD + 1)
    assert runtime.available
    assert changes == []
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---- structure watch ----


def _features_structure(states: Any) -> frozenset[tuple[str, str, str | None, tuple[str, ...]]]:
    return frozenset(("sensor", f, None, ()) for f in states["info/features"])


async def test_structure_change_reloads(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A changed entity set reloads the entry once, 15 s after the last change."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    with patch("custom_components.tinkerforge_warp.structure", _features_structure):
        await _setup(hass, entry)
        features = fake.states["info/features"]

        with patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=hass.config_entries.async_schedule_reload,
        ) as reload:
            # Unrelated and unchanged pushes do not reload.
            await fake.push("info/features", features)
            await fake.push("info/keep_alive", {"uptime": 1})
            await _wait_for(
                lambda: _runtime(entry).client.states["info/keep_alive"] == {"uptime": 1}
            )
            await _fire(hass, RELOAD_DEBOUNCE + 1)
            reload.assert_not_called()

            # Two quick changes: debounced into one reload.
            await fake.push("info/features", [*features, "extra"])
            await _wait_for(lambda: "extra" in _runtime(entry).client.states["info/features"])
            await _fire(hass, RELOAD_DEBOUNCE - 5)
            await fake.push("meters/1/config", [0, None])
            await _wait_for(lambda: _runtime(entry).client.states["meters/1/config"] == [0, None])
            await _fire(hass, RELOAD_DEBOUNCE - 5)
            reload.assert_not_called()
            await _fire(hass, RELOAD_DEBOUNCE + 1)
            reload.assert_called_once_with(entry.entry_id)

        await _wait_for(lambda: fake.ws_connect_count == 2)
        await _wait_for(lambda: entry.state is ConfigEntryState.LOADED)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_structure_unchanged_after_reconnect(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A reconnect with the same structure does not reload."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    with (
        patch("custom_components.tinkerforge_warp.structure", _features_structure),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await _setup(hass, entry)
        await fake.drop_clients(1006)
        await _wait_for(lambda: fake.ws_connect_count == 2 and _runtime(entry).client.connected)
        await _fire(hass, RELOAD_DEBOUNCE + 1)
        reload.assert_not_called()
        await hass.config_entries.async_unload(entry.entry_id)


async def test_structure_checked_after_reconnect(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A structure change that happened while offline is picked up on reconnect."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    with (
        patch("custom_components.tinkerforge_warp.structure", _features_structure),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await _setup(hass, entry)
        fake.states["info/features"] = [*fake.states["info/features"], "extra"]
        await fake.drop_clients(1006)
        await _wait_for(lambda: "extra" in _runtime(entry).client.states["info/features"])
        await _fire(hass, RELOAD_DEBOUNCE + 1)
        reload.assert_called_once_with(entry.entry_id)
        await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_devices_removed(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Sub-devices without any entity are removed; used ones and other devices stay."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "2jEx_meter_3")}
    )
    used = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "2jEx_meter_1")}
    )
    shared = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("other", "x")}
    )
    with patch(
        "custom_components.tinkerforge_warp.structure",
        lambda _states: frozenset({("sensor", "power", "meter_1", ())}),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert registry.async_get(stale.id) is None
    assert registry.async_get(used.id) is not None
    assert registry.async_get(shared.id) is None  # unknown identifiers are dropped too
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---- repair issue ----


async def test_too_many_clients_issue(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Repeated kicks raise a repair issue that goes away once they stop."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    issue_id = f"too_many_clients_{entry.entry_id}"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    client = _runtime(entry).client
    for count in range(1, 4):
        await fake.drop_clients(1006)
        await _wait_for(lambda count=count: fake.ws_connect_count == count + 1 and client.connected)
    assert client.kicked_repeatedly
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "too_many_clients"
    assert issue.translation_placeholders == {"name": "warp3-charger-pro"}

    with patch.object(WarpClient, "kicked_repeatedly", PropertyMock(return_value=False)):
        await fake.drop_clients(1006)
        await _wait_for(lambda: fake.ws_connect_count == 5 and client.connected)
        assert issues.async_get_issue(DOMAIN, issue_id) is None

    # Unload removes the issue as well.
    with patch.object(WarpClient, "kicked_repeatedly", PropertyMock(return_value=True)):
        await fake.drop_clients(1006)
        await _wait_for(lambda: issues.async_get_issue(DOMAIN, issue_id) is not None)
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert issues.async_get_issue(DOMAIN, issue_id) is None


# ---- WarpRuntime.async_put ----


async def test_async_put_success(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Commands reach the device."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    await _runtime(entry).async_put("evse/external_current", {"current": 8000})
    assert fake.puts[-1] == ("evse/external_current", {"current": 8000})
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_put_with_credentials(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Commands are digest-authenticated even though the WebSocket connected first."""
    fake = await fake_warp("warp3", auth=AUTH)
    entry = _entry(fake, **{CONF_USERNAME: "admin", CONF_PASSWORD: "secret"})
    await _setup(hass, entry)
    await _runtime(entry).async_put("evse/external_current", {"current": 8000})
    assert fake.puts[-1] == ("evse/external_current", {"current": 8000})
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_async_put_errors(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Device and network errors become translated HomeAssistantErrors."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    runtime = _runtime(entry)

    with pytest.raises(HomeAssistantError) as exc:
        await runtime.async_put("evse/external_current", {"current": 70000})
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "command_failed"
    assert exc.value.translation_placeholders == {"error": "Integer value out of range"}

    with pytest.raises(HomeAssistantError) as exc:
        await runtime.async_put("does/not/exist")  # 404 -> unexpected answer
    assert exc.value.translation_key == "cannot_connect"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_async_put_auth_error(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A 401 on a command starts reauth."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    fake.auth = AUTH
    with pytest.raises(HomeAssistantError) as exc:
        await _runtime(entry).async_put("reboot")
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "invalid_auth"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    await hass.config_entries.async_unload(entry.entry_id)


# ---- WarpEntity ----


@dataclass(frozen=True, kw_only=True)
class _Description(WarpEntityDescription, SensorEntityDescription):
    """Description combining the WARP and sensor bases, like the platform modules do."""


class _TestSensor(WarpEntity, SensorEntity):
    updates: list[Any]

    def __init__(self, runtime: WarpRuntime, description: WarpEntityDescription) -> None:
        super().__init__(runtime, description)
        self.updates = []

    def _update_from_value(self, value: Any) -> None:
        self.updates.append(value)
        self._attr_native_value = value


def _descriptions() -> list[_Description]:
    state = _Description(
        key="charger_state",
        translation_key="charger_state",
        topics=("evse/state",),
        value_fn=lambda s: s["evse/state"]["charger_state"],
        available_fn=lambda s: s["evse/state"]["error_state"] != 99,
    )
    return [
        state,
        replace(
            state,
            key="broken",
            value_fn=lambda s: s["evse/nope"]["x"],
            available_fn=lambda s: s["evse/nope"]["x"] > 0,
        ),
        replace(
            state,
            key="meter_power",
            value_fn=lambda s: s["evse/state"]["charger_state"] / 0,
            available_fn=None,
            device="meter_0",
        ),
    ]


@pytest.fixture
async def sensors(hass: HomeAssistant) -> AsyncGenerator[list[_TestSensor]]:
    """Register a fake sensor platform built on WarpEntity."""
    await async_get_integration(hass, DOMAIN)  # load the real one before mocking a platform
    created: list[_TestSensor] = []

    async def async_setup_entry(
        hass: HomeAssistant, entry: WarpConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        created.extend(_TestSensor(entry.runtime_data, d) for d in _descriptions())
        async_add_entities(created)

    platform = MockPlatform(async_setup_entry=async_setup_entry)
    platform.descriptions = lambda _states: _descriptions()
    mock_platform(hass, f"{DOMAIN}.sensor", platform)
    with patch(
        "custom_components.tinkerforge_warp.platform_modules",
        lambda: {Platform.SENSOR: platform},
    ):
        yield created


async def test_entity_lifecycle(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, sensors: list[_TestSensor]
) -> None:
    """Values follow topics; writes happen only on change; errors map to unknown/unavailable."""
    fake = await fake_warp("warp3")
    entry = _entry(fake)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    state_entity, broken, meter = sensors

    assert state_entity.unique_id == "2jEx_charger_state"
    assert state_entity.device_info["identifiers"] == {(DOMAIN, "2jEx")}
    assert meter.device_info["identifiers"] == {(DOMAIN, "2jEx_meter_0")}
    assert meter.device_info["name"] == "WARP Charger"
    main = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "2jEx"), entry.entry_id)
    assert meter.device_info["via_device_id"] == main.id
    assert entry.runtime_data.device_info("meter_9")["name"] == "Meter 9"

    initial = fake.states["evse/state"]["charger_state"]
    assert state_entity.updates == [initial]
    assert hass.states.get(state_entity.entity_id).state == str(initial)
    assert hass.states.get(broken.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(meter.entity_id).state == STATE_UNKNOWN

    # A change in an unrelated field of the topic does not touch the entity.
    other = {**fake.states["evse/state"], "allowed_charging_current": 123}
    await fake.push("evse/state", other)
    await _wait_for(lambda: _runtime(entry).client.states["evse/state"] == other)
    assert state_entity.updates == [initial]

    # A real change does.
    changed = {**other, "charger_state": 2}
    await fake.push("evse/state", changed)
    await _wait_for(lambda: state_entity.updates == [initial, 2])
    assert hass.states.get(state_entity.entity_id).state == "2"

    # available_fn makes it unavailable and back.
    await fake.push("evse/state", {**changed, "error_state": 99})
    await _wait_for(lambda: hass.states.get(state_entity.entity_id).state == STATE_UNAVAILABLE)
    await fake.push("evse/state", changed)
    await _wait_for(lambda: hass.states.get(state_entity.entity_id).state == "2")

    # Grace period expiry makes everything unavailable; reconnect restores it.
    fake.hold_dump = True
    await fake.drop_clients(1006)
    await _wait_for(lambda: not _runtime(entry).client.connected)
    assert hass.states.get(state_entity.entity_id).state == "2"
    await _fire(hass, GRACE_PERIOD + 1)
    assert hass.states.get(state_entity.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(meter.entity_id).state == STATE_UNAVAILABLE
    fake.hold_dump = False
    await fake.drop_clients(1006)
    await _wait_for(lambda: hass.states.get(state_entity.entity_id).state == "2")
    assert hass.states.get(meter.entity_id).state == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.states.get(state_entity.entity_id).state == STATE_UNAVAILABLE
