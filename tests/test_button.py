"""Button platform tests."""

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.tinkerforge_warp.const import DOMAIN, GRACE_PERIOD
from tests.common import (
    FIXTURES,
    check_availability_cycle,
    entity_id,
    fire,
    get_state,
    platform_fixture,
    reject,
    runtime_of,
    setup_entry,
    wait_for,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import FakeWarp

warp_platform = platform_fixture(Platform.BUTTON)

BUTTONS = [
    ("start_charging", "evse/start_charging"),
    ("stop_charging", "evse/stop_charging"),
    ("restart_charge_limits", "charge_limits/restart"),
    ("reboot", "reboot"),
]


async def _press(hass: HomeAssistant, fake: FakeWarp, key: str) -> None:
    eid = entity_id(hass, "button", fake, key)
    assert eid is not None
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: eid}, blocking=True
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("fixture", FIXTURES)
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """All buttons of each device model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_energy_manager_only_reboots(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Without EVSE (and without charge limits) only the reboot button exists."""
    fake = await fake_warp("wem2")
    entry = await setup_entry(hass, fake)
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert [e.unique_id for e in entries] == ["2gNm_reboot"]


@pytest.mark.parametrize(("key", "path"), BUTTONS[:3])
async def test_press_sends_null(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, key: str, path: str
) -> None:
    """Pressing sends a PUT with a null body."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await _press(hass, fake, key)
    assert fake.puts == [(path, None)]
    assert get_state(hass, "button", fake, key).state != STATE_UNAVAILABLE


async def test_start_stop_with_car(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Start/stop move the button slot; without a car they are silent no-ops."""
    fake = await fake_warp("warp2")
    await setup_entry(hass, fake)
    await _press(hass, fake, "stop_charging")  # no car: nothing happens, still 200
    assert fake.states["evse/slots"][4]["max_current"] == 32000
    await fake.plug_in()
    await _press(hass, fake, "stop_charging")
    assert fake.states["evse/slots"][4]["max_current"] == 0
    await _press(hass, fake, "start_charging")
    assert fake.states["evse/slots"][4]["max_current"] == 32000
    assert [p[0] for p in fake.puts] == [
        "evse/stop_charging",
        "evse/stop_charging",
        "evse/start_charging",
    ]


async def test_restart_charge_limits_without_session(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Restarting the limits is harmless without a running session."""
    fake = await fake_warp("warp2")
    await setup_entry(hass, fake)
    await _press(hass, fake, "restart_charge_limits")
    assert fake.puts == [("charge_limits/restart", None)]


@pytest.mark.parametrize(("key", "path"), BUTTONS)
async def test_command_failed(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, key: str, path: str
) -> None:
    """A rejected command raises a translated error."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    reject(fake, path, "Busy")
    with pytest.raises(HomeAssistantError) as exc:
        await _press(hass, fake, key)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "command_failed"
    assert exc.value.translation_placeholders == {"error": "Busy"}


async def test_reboot_within_grace_period(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Reboot: sockets close with 1001, entities stay available, the client reconnects."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    client = runtime_of(hass).client
    ids = [entity_id(hass, "button", fake, key) or "" for key, _ in BUTTONS]

    await _press(hass, fake, "reboot")
    assert fake.puts == [("reboot", None)]
    await wait_for(lambda: fake.reboot_count == 1)
    await wait_for(lambda: fake.ws_connect_count == 2 and client.connected)
    await hass.async_block_till_done()
    await fire(hass, GRACE_PERIOD + 1)
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in ids)
    assert runtime_of(hass).available
    assert not client.kicked_repeatedly  # a 1001 close is not a kick


async def test_reboot_longer_than_grace_period(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A device that stays away longer than 30 s is reported unavailable, then recovers."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    client = runtime_of(hass).client
    ids = [entity_id(hass, "button", fake, key) or "" for key, _ in BUTTONS]

    fake.hold_dump = True  # the device is booting: no complete dump yet
    await _press(hass, fake, "reboot")
    await wait_for(lambda: fake.reboot_count == 1)
    await wait_for(lambda: not client.connected)
    await fire(hass, GRACE_PERIOD - 1)
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in ids)
    await fire(hass, GRACE_PERIOD + 1)  # fire() jumps relative to now, not to the last call
    assert all(hass.states.get(e).state == STATE_UNAVAILABLE for e in ids)

    fake.hold_dump = False
    await fake.drop_clients(1006)
    await wait_for(lambda: client.connected)
    await hass.async_block_till_done()
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in ids)


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    ids = [entity_id(hass, "button", fake, key) or "" for key, _ in BUTTONS]
    await check_availability_cycle(hass, fake, ids)
