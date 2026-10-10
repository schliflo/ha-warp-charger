"""Binary sensor platform tests."""

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from tests.common import (
    FIXTURES,
    check_availability_cycle,
    entity_id,
    get_state,
    merge,
    platform_fixture,
    push,
    setup_entry,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import REMOVE, FakeWarp

warp_platform = platform_fixture(Platform.BINARY_SENSOR)


def _state(hass: HomeAssistant, fake: FakeWarp, key: str) -> str:
    return get_state(hass, "binary_sensor", fake, key).state


@pytest.mark.parametrize("fixture", FIXTURES)
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """All binary sensors of each device model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.mark.parametrize(
    ("charger_state", "error_state", "plugged", "charging", "problem"),
    [
        (0, 0, STATE_OFF, STATE_OFF, STATE_OFF),  # not connected
        (1, 0, STATE_ON, STATE_OFF, STATE_OFF),  # waiting for release
        (2, 0, STATE_ON, STATE_OFF, STATE_OFF),  # ready
        (3, 0, STATE_ON, STATE_ON, STATE_OFF),  # charging
        (4, 2, STATE_OFF, STATE_OFF, STATE_ON),  # error state with an error code
        (4, 0, STATE_OFF, STATE_OFF, STATE_ON),  # charger_state error alone is a problem
        (0, 5, STATE_OFF, STATE_OFF, STATE_ON),  # error code alone is a problem
        (3, 3, STATE_ON, STATE_ON, STATE_ON),
    ],
)
async def test_charger_binary_sensors(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    *,
    charger_state: int,
    error_state: int,
    plugged: str,
    charging: str,
    problem: str,
) -> None:
    """plugged / charging / problem derive from charger_state and error_state."""
    fake = await fake_warp("warp3", patches={"evse/state": {"charger_state": 0}})
    await setup_entry(hass, fake)
    await merge(hass, fake, "evse/state", charger_state=charger_state, error_state=error_state)
    assert _state(hass, fake, "plugged") == plugged
    assert _state(hass, fake, "charging") == charging
    assert _state(hass, fake, "problem") == problem


async def test_initial_states(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The real WARP3 dump: plugged, waiting for release, no problem."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "plugged") == STATE_ON
    assert _state(hass, fake, "charging") == STATE_OFF
    assert _state(hass, fake, "problem") == STATE_OFF
    assert _state(hass, fake, "button_pressed") == STATE_OFF
    assert _state(hass, fake, "p14a_limited") == STATE_OFF


async def test_button_pressed(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The physical button state."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await merge(hass, fake, "evse/button_state", button_pressed=True)
    assert _state(hass, fake, "button_pressed") == STATE_ON
    await merge(hass, fake, "evse/button_state", button_pressed=False)
    assert _state(hass, fake, "button_pressed") == STATE_OFF


async def test_p14a_limited(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The section 14a limit is active."""
    fake = await fake_warp("wem2")
    await setup_entry(hass, fake)
    assert entity_id(hass, "binary_sensor", fake, "plugged") is None
    assert _state(hass, fake, "p14a_limited") == STATE_OFF
    await merge(hass, fake, "p14a_enwg/state", active=True)
    assert _state(hass, fake, "p14a_limited") == STATE_ON


async def test_p14a_absent_without_config(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """No section 14a config, no section 14a entity."""
    fake = await fake_warp("wem2", patches={"p14a_enwg/config": REMOVE})
    entry = await setup_entry(hass, fake)
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []


async def test_firmware_drift(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A payload without the expected keys makes the sensors unknown."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await push(hass, fake, "evse/state", {"iec61851_state": 1})
    for key in ("plugged", "charging", "problem"):
        assert _state(hass, fake, key) == "unknown"
    await push(hass, fake, "evse/button_state", {"button_pressed": None})
    assert _state(hass, fake, "button_pressed") == "unknown"


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    ids = [entity_id(hass, "binary_sensor", fake, k) or "" for k in ("plugged", "problem")]
    await check_availability_cycle(hass, fake, ids)
    assert _state(hass, fake, "plugged") == STATE_ON
