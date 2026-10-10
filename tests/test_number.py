"""Number platform tests."""

import pytest
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.tinkerforge_warp.const import DOMAIN
from tests.common import (
    check_availability_cycle,
    entity_id,
    get_state,
    merge,
    platform_fixture,
    push,
    reject,
    setup_entry,
    wait_for,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import FakeWarp

warp_platform = platform_fixture(Platform.NUMBER)


async def _set(hass: HomeAssistant, fake: FakeWarp, key: str, value: float) -> None:
    eid = entity_id(hass, "number", fake, key)
    assert eid is not None
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: eid, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, fake: FakeWarp, key: str) -> str:
    return get_state(hass, "number", fake, key).state


@pytest.mark.parametrize("fixture", ["warp3", "warp2", "warp4"])
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """All numbers of each charger model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_no_numbers_without_evse(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The energy manager has nothing to set."""
    fake = await fake_warp("wem2")
    entry = await setup_entry(hass, fake)
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []


@pytest.mark.parametrize("amps", [0, 6, 10, 16])
async def test_charging_current_limit(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, amps: int
) -> None:
    """0 pauses, 6..max A are sent in mA, and the device echo updates the state."""
    fake = await fake_warp("warp3", patches={"evse/external_current": {"current": 20000}})
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charging_current_limit") == "20.0"

    await _set(hass, fake, "charging_current_limit", amps)
    assert fake.puts == [("evse/external_current", {"current": amps * 1000})]
    assert fake.states["evse/external_current"] == {"current": amps * 1000}
    assert _state(hass, fake, "charging_current_limit") == f"{float(amps)}"


async def test_charging_current_limit_too_low(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """1..5 A is not a valid current: validation error, nothing is sent."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    for amps in (1, 3, 5):
        with pytest.raises(ServiceValidationError) as exc:
            await _set(hass, fake, "charging_current_limit", amps)
        assert exc.value.translation_domain == DOMAIN
        assert exc.value.translation_key == "current_too_low"
    assert fake.puts == []
    assert _state(hass, fake, "charging_current_limit") == "0.0"


async def test_charging_current_limit_availability(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Unavailable while external control is disabled on the device (old firmware)."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await push(hass, fake, "evse/external_enabled", {"enabled": False})
    assert _state(hass, fake, "charging_current_limit") == STATE_UNAVAILABLE
    await push(hass, fake, "evse/external_enabled", {"enabled": True})
    assert _state(hass, fake, "charging_current_limit") == "0.0"
    await push(hass, fake, "evse/external_enabled", {})  # drift: key missing
    assert _state(hass, fake, "charging_current_limit") == STATE_UNAVAILABLE


async def test_charging_current_limit_follows_device(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Changes made elsewhere (web UI, evcc) show up."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await push(hass, fake, "evse/external_current", {"current": 10500})
    assert _state(hass, fake, "charging_current_limit") == "10.5"


async def test_charge_energy_limit(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """kWh in the UI, Wh on the wire; 0 means off."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_energy_limit") == "0.0"
    await _set(hass, fake, "charge_energy_limit", 7.5)
    assert fake.puts == [("charge_limits/override_energy", {"energy_wh": 7500})]
    assert _state(hass, fake, "charge_energy_limit") == "7.5"
    await _set(hass, fake, "charge_energy_limit", 0)
    assert fake.puts[-1] == ("charge_limits/override_energy", {"energy_wh": 0})
    assert _state(hass, fake, "charge_energy_limit") == "0.0"


async def test_charge_soc_limit_and_vehicle_soc(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """WARP4: SoC limit and the injectable vehicle SoC."""
    fake = await fake_warp("warp4")
    await setup_entry(hass, fake)
    assert _state(hass, fake, "charge_soc_limit") == "0"
    assert _state(hass, fake, "vehicle_soc") == "55.5"

    await _set(hass, fake, "charge_soc_limit", 80)
    assert fake.puts == [("charge_limits/override_soc", {"soc_pct": 80})]
    assert _state(hass, fake, "charge_soc_limit") == "80"

    await _set(hass, fake, "vehicle_soc", 70.5)
    assert fake.puts[-1] == ("ev/inject_soc", {"soc": 70.5})
    assert _state(hass, fake, "vehicle_soc") == "70.5"


async def test_vehicle_soc_unknown(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A null SoC (no vehicle data) is unknown."""
    fake = await fake_warp("warp4")
    await setup_entry(hass, fake)
    await merge(hass, fake, "ev/state", soc=None)
    assert _state(hass, fake, "vehicle_soc") == STATE_UNKNOWN


async def test_soc_limit_only_with_soc_key(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Only WARP4 firmware has soc_pct in the limits; there is no vehicle SoC without ev/state."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert entity_id(hass, "number", fake, "charge_energy_limit") is not None
    assert entity_id(hass, "number", fake, "charge_soc_limit") is None
    assert entity_id(hass, "number", fake, "vehicle_soc") is None


@pytest.mark.parametrize(
    ("fixture", "key", "path", "value"),
    [
        ("warp3", "charging_current_limit", "evse/external_current_update", 16),
        ("warp3", "charge_energy_limit", "charge_limits/override_energy", 5),
        ("warp4", "charge_soc_limit", "charge_limits/override_soc", 50),
        ("warp4", "vehicle_soc", "ev/inject_soc", 40),
    ],
)
async def test_command_failed(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    *,
    fixture: str,
    key: str,
    path: str,
    value: float,
) -> None:
    """A 400 from the device becomes a translated HomeAssistantError."""
    fake = await fake_warp(fixture)
    await setup_entry(hass, fake)
    reject(fake, path, "Nope, because")
    before = _state(hass, fake, key)
    with pytest.raises(HomeAssistantError) as exc:
        await _set(hass, fake, key, value)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "command_failed"
    assert exc.value.translation_placeholders == {"error": "Nope, because"}
    assert len(fake.puts) == 1
    assert _state(hass, fake, key) == before


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    ids = [
        entity_id(hass, "number", fake, k) or ""
        for k in ("charging_current_limit", "charge_energy_limit")
    ]
    await check_availability_cycle(hass, fake, ids)
    await wait_for(lambda: _state(hass, fake, "charging_current_limit") == "0.0")
