"""Select platform tests."""

import pytest
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.tinkerforge_warp.const import DOMAIN
from tests.common import (
    FIXTURES,
    check_availability_cycle,
    entity_id,
    get_state,
    merge,
    platform_fixture,
    push,
    reject,
    setup_entry,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import REMOVE, FakeWarp

warp_platform = platform_fixture(Platform.SELECT)

MANAGED_CHARGER = {
    "evse/management_enabled": {"enabled": True},
    "evse/supported_charge_modes": [0, 1, 2],
    "evse/charge_mode": {"mode": 0},
}


async def _select(hass: HomeAssistant, fake: FakeWarp, key: str, option: str) -> None:
    eid = entity_id(hass, "select", fake, key)
    assert eid is not None
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: eid, ATTR_OPTION: option},
        blocking=True,
    )
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, fake: FakeWarp, key: str) -> str:
    return get_state(hass, "select", fake, key).state


def _options(hass: HomeAssistant, fake: FakeWarp, key: str) -> list[str]:
    return get_state(hass, "select", fake, key).attributes[ATTR_OPTIONS]


@pytest.mark.parametrize("fixture", FIXTURES)
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """All selects of each device model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_snapshot_managed_charger(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
) -> None:
    """WARP3 with the charge management of the charger itself enabled."""
    fake = await fake_warp("warp3", patches=MANAGED_CHARGER)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_charge_mode_absent_on_plain_charger(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Without charge management there is nothing to select."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert entity_id(hass, "select", fake, "charge_mode") is None


async def test_charge_mode_manager(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Energy manager: options from charge_manager/supported_charge_modes, set via power_manager."""
    fake = await fake_warp("wem2")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_mode") == "pv"
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv", "min_pv"]

    await _select(hass, fake, "charge_mode", "fast")
    assert fake.puts == [("power_manager/charge_mode_update", {"mode": 0})]
    assert fake.states["power_manager/charge_mode"] == {"mode": 0}
    assert _state(hass, fake, "charge_mode") == "fast"

    # More modes become available (e.g. dynamic tariff enabled): options follow, no reload.
    await push(hass, fake, "charge_manager/supported_charge_modes", [0, 1, 2, 3, 5, 6, 7, 8, 9])
    assert _options(hass, fake, "charge_mode") == [
        "fast",
        "off",
        "pv",
        "min_pv",
        "min",
        "eco",
        "eco_pv",
        "eco_min",
        "eco_min_pv",
    ]
    await _select(hass, fake, "charge_mode", "eco_min_pv")
    assert fake.puts[-1] == ("power_manager/charge_mode_update", {"mode": 9})
    assert _state(hass, fake, "charge_mode") == "eco_min_pv"

    # Mode 4 (Default) is a state of the device but never an option.
    await push(hass, fake, "power_manager/charge_mode", {"mode": 4})
    assert _state(hass, fake, "charge_mode") == STATE_UNKNOWN
    assert "default" not in _options(hass, fake, "charge_mode")

    # A mode that is no longer supported is not shown either.
    await push(hass, fake, "power_manager/charge_mode", {"mode": 9})
    await push(hass, fake, "charge_manager/supported_charge_modes", [0, 1])
    assert _options(hass, fake, "charge_mode") == ["fast", "off"]
    assert _state(hass, fake, "charge_mode") == STATE_UNKNOWN

    # Unknown mode numbers are ignored.
    await push(hass, fake, "charge_manager/supported_charge_modes", [1, 42, 0])
    assert _options(hass, fake, "charge_mode") == ["fast", "off"]
    await push(hass, fake, "power_manager/charge_mode", {"mode": 42})
    assert _state(hass, fake, "charge_mode") == STATE_UNKNOWN


async def test_charge_mode_managed_charger(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A charger managed by the charge manager of another device uses the evse topics."""
    fake = await fake_warp("warp3", patches=MANAGED_CHARGER)
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_mode") == "fast"
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv"]

    await _select(hass, fake, "charge_mode", "pv")
    assert fake.puts == [("evse/charge_mode_update", {"mode": 2})]
    assert fake.states["evse/charge_mode"] == {"mode": 2}
    assert _state(hass, fake, "charge_mode") == "pv"

    await push(hass, fake, "evse/supported_charge_modes", [0, 1, 2, 3])
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv", "min_pv"]
    await push(hass, fake, "evse/supported_charge_modes", [])
    assert _options(hass, fake, "charge_mode") == []
    assert _state(hass, fake, "charge_mode") == STATE_UNKNOWN


async def test_charge_mode_prefers_charge_manager(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """The charge manager wins when both are enabled."""
    fake = await fake_warp(
        "wem2",
        patches={
            "evse/management_enabled": {"enabled": True},
            "evse/charge_mode": {"mode": 1},
            "evse/supported_charge_modes": [1],
            "info/features": ["evse", "meters"],
        },
    )
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_mode") == "pv"
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv", "min_pv"]


async def test_charge_mode_firmware_drift(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Missing payload keys make the select unknown with no options."""
    fake = await fake_warp("wem2")
    await setup_entry(hass, fake)
    await push(hass, fake, "power_manager/charge_mode", {})
    assert _state(hass, fake, "charge_mode") == STATE_UNKNOWN
    assert _options(hass, fake, "charge_mode") == []
    await push(hass, fake, "power_manager/charge_mode", {"mode": 2})
    assert _state(hass, fake, "charge_mode") == "pv"
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv", "min_pv"]


async def test_charge_mode_without_supported_modes_topic(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Firmware drift at startup: the select exists but has no options yet."""
    fake = await fake_warp("wem2", patches={"charge_manager/supported_charge_modes": REMOVE})
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_mode") == STATE_UNKNOWN
    assert _options(hass, fake, "charge_mode") == []
    await push(hass, fake, "charge_manager/supported_charge_modes", [0, 1, 2])
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv"]
    assert _state(hass, fake, "charge_mode") == "pv"


async def test_phases(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Request 1- or 3-phase charging; only while external control is available."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "phases") == "single_phase"
    assert _options(hass, fake, "phases") == ["single_phase", "three_phase"]

    await _select(hass, fake, "phases", "three_phase")
    assert fake.puts == [("power_manager/external_control_update", {"phases_wanted": 3})]
    assert fake.states["power_manager/external_control"] == {"phases_wanted": 3}
    assert _state(hass, fake, "phases") == "three_phase"
    await _select(hass, fake, "phases", "single_phase")
    assert fake.puts[-1] == ("power_manager/external_control_update", {"phases_wanted": 1})
    assert _state(hass, fake, "phases") == "single_phase"


@pytest.mark.parametrize("external_control", [1, 2, 3])
async def test_phases_unavailable(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, external_control: int
) -> None:
    """Disabled, in error or switching: not available; available again once it is 0."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await merge(hass, fake, "power_manager/state", external_control=external_control)
    assert _state(hass, fake, "phases") == STATE_UNAVAILABLE
    await merge(hass, fake, "power_manager/state", external_control=0)
    assert _state(hass, fake, "phases") == "single_phase"


async def test_phases_unknown_value(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """phases_wanted 0 (no request yet) is unknown."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await push(hass, fake, "power_manager/external_control", {"phases_wanted": 0})
    assert _state(hass, fake, "phases") == STATE_UNKNOWN


async def test_phases_rejected_by_device(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The device refuses while external control is disabled (race with availability)."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    fake.states["power_manager/state"] = {
        **fake.states["power_manager/state"],
        "external_control": 1,
    }
    with pytest.raises(HomeAssistantError) as exc:
        await _select(hass, fake, "phases", "three_phase")
    assert exc.value.translation_key == "command_failed"
    assert "External control is not enabled" in exc.value.translation_placeholders["error"]


async def test_no_phases_without_phase_switch(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """WARP2 and the energy manager cannot switch phases."""
    for fixture in ("warp2", "wem2"):
        fake = await fake_warp(fixture)
        await setup_entry(hass, fake)
        assert entity_id(hass, "select", fake, "phases") is None
        await hass.config_entries.async_unload(
            hass.config_entries.async_entries(DOMAIN)[0].entry_id
        )
        await hass.config_entries.async_remove(
            hass.config_entries.async_entries(DOMAIN)[0].entry_id
        )


async def test_charge_duration_limit(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Every duration maps to the device's enum value."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_duration_limit") == "off"
    options = _options(hass, fake, "charge_duration_limit")
    assert options == ["off", "15m", "30m", "45m", "1h", "2h", "3h", "4h", "6h", "8h", "12h"]

    for value, option in enumerate(options):
        await _select(hass, fake, "charge_duration_limit", option)
        assert fake.puts[-1] == ("charge_limits/override_duration", {"duration": value})
        assert fake.states["charge_limits/active_limits"]["duration"] == value
        assert _state(hass, fake, "charge_duration_limit") == option
    assert len(fake.puts) == len(options)

    await merge(hass, fake, "charge_limits/active_limits", duration=11)  # not in the table
    assert _state(hass, fake, "charge_duration_limit") == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("fixture", "key", "option", "path"),
    [
        ("warp3", "phases", "three_phase", "power_manager/external_control_update"),
        ("warp3", "charge_duration_limit", "1h", "charge_limits/override_duration"),
        ("wem2", "charge_mode", "fast", "power_manager/charge_mode_update"),
    ],
)
async def test_command_failed(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    *,
    fixture: str,
    key: str,
    option: str,
    path: str,
) -> None:
    """A 400 becomes a translated HomeAssistantError and the state stays."""
    fake = await fake_warp(fixture)
    await setup_entry(hass, fake)
    reject(fake, path, "Cannot do that")
    before = _state(hass, fake, key)
    with pytest.raises(HomeAssistantError) as exc:
        await _select(hass, fake, key, option)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "command_failed"
    assert exc.value.translation_placeholders == {"error": "Cannot do that"}
    assert _state(hass, fake, key) == before


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3", patches=MANAGED_CHARGER)
    await setup_entry(hass, fake)
    ids = [
        entity_id(hass, "select", fake, key) or ""
        for key in ("charge_mode", "phases", "charge_duration_limit")
    ]
    await check_availability_cycle(hass, fake, ids)
    assert _state(hass, fake, "charge_mode") == "fast"
    assert _options(hass, fake, "charge_mode") == ["fast", "off", "pv"]
