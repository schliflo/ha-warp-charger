"""Switch platform tests."""

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.tinkerforge_warp.const import DOMAIN
from tests.common import (
    check_availability_cycle,
    entity_id,
    get_state,
    platform_fixture,
    push,
    reject,
    setup_entry,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import FakeWarp

warp_platform = platform_fixture(Platform.SWITCH)

SWITCHES = [
    ("auto_start_charging", "evse/auto_start_charging", "auto_start_charging", True),
    (
        "external_clear_on_disconnect",
        "evse/external_clear_on_disconnect",
        "clear_on_disconnect",
        False,
    ),
    ("boost_mode", "evse/boost_mode", "enabled", False),
    ("phase_auto_switch", "evse/phase_auto_switch", "enabled", False),
]


async def _call(hass: HomeAssistant, fake: FakeWarp, key: str, service: str) -> None:
    eid = entity_id(hass, "switch", fake, key)
    assert eid is not None
    await hass.services.async_call("switch", service, {ATTR_ENTITY_ID: eid}, blocking=True)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, fake: FakeWarp, key: str) -> str:
    return get_state(hass, "switch", fake, key).state


@pytest.mark.parametrize("fixture", ["warp3", "warp2", "warp4"])
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """All switches of each charger model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_no_switches_without_evse(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The energy manager has none."""
    fake = await fake_warp("wem2")
    entry = await setup_entry(hass, fake)
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []


async def test_phase_auto_switch_needs_warp3_hardware(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """EVSE version 20 (WARP2) cannot switch phases."""
    fake = await fake_warp("warp2")
    await setup_entry(hass, fake)
    assert entity_id(hass, "switch", fake, "phase_auto_switch") is None
    assert entity_id(hass, "switch", fake, "boost_mode") is not None


@pytest.mark.parametrize(("key", "topic", "member", "initial"), SWITCHES)
async def test_switch_on_off(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    *,
    key: str,
    topic: str,
    member: str,
    initial: bool,
) -> None:
    """Turning on/off sends '<topic>_update' and the state follows the device echo."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, key) == (STATE_ON if initial else STATE_OFF)

    for on in (not initial, initial):
        await _call(hass, fake, key, SERVICE_TURN_ON if on else SERVICE_TURN_OFF)
        assert fake.puts[-1] == (f"{topic}_update", {member: on})
        assert fake.states[topic][member] is on
        assert _state(hass, fake, key) == (STATE_ON if on else STATE_OFF)
    assert len(fake.puts) == 2


async def test_switch_follows_device(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Changes from the device UI are reflected; missing keys give unknown."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await push(hass, fake, "evse/boost_mode", {"enabled": True})
    assert _state(hass, fake, "boost_mode") == STATE_ON
    await push(hass, fake, "evse/boost_mode", {})
    assert _state(hass, fake, "boost_mode") == STATE_UNKNOWN
    await push(hass, fake, "evse/boost_mode", {"enabled": None})
    assert _state(hass, fake, "boost_mode") == STATE_UNKNOWN


async def test_auto_start_side_effects(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Disabling auto start leaves the other switches alone."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await _call(hass, fake, "auto_start_charging", SERVICE_TURN_OFF)
    assert _state(hass, fake, "auto_start_charging") == STATE_OFF
    assert _state(hass, fake, "boost_mode") == STATE_OFF


@pytest.mark.parametrize(("key", "topic", "member", "initial"), SWITCHES)
async def test_command_failed(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    *,
    key: str,
    topic: str,
    member: str,
    initial: bool,
) -> None:
    """A rejected command raises a translated error and leaves the state alone."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    reject(fake, f"{topic}_update", "Not today")
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, fake, key, SERVICE_TURN_OFF if initial else SERVICE_TURN_ON)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "command_failed"
    assert exc.value.translation_placeholders == {"error": "Not today"}
    assert _state(hass, fake, key) == (STATE_ON if initial else STATE_OFF)


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    ids = [entity_id(hass, "switch", fake, s[0]) or "" for s in SWITCHES]
    await check_availability_cycle(hass, fake, ids)
    assert _state(hass, fake, "auto_start_charging") == STATE_ON
