"""Number platform: charging current, charge limits and vehicle SoC."""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import (
    VALUE_ERRORS,
    States,
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    WarpRuntime,
    has_feature,
)

PARALLEL_UPDATES = 0

MIN_CURRENT = 6  # A; the device accepts 0 (pause) or 6..32 A
# evse/hardware_configuration.jumper_configuration -> max current (A); 7 = software, 8 = unset
JUMPER_MAX_CURRENT = {0: 6, 1: 10, 2: 13, 3: 16, 4: 20, 5: 25, 6: 32}


def _max_current(states: States) -> float:
    jumper = states["evse/hardware_configuration"]["jumper_configuration"]
    return JUMPER_MAX_CURRENT.get(jumper, 32)


@dataclass(frozen=True, kw_only=True)
class WarpNumberEntityDescription(WarpEntityDescription, NumberEntityDescription):
    """Number description with the command it sends."""

    put_path: str
    payload_fn: Callable[[float], Any]
    max_fn: Callable[[States], float] | None = None


def _current_payload(value: float) -> dict[str, int]:
    if 0 < value < MIN_CURRENT:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="current_too_low")
    return {"current": round(value * 1000)}


CHARGING_CURRENT_LIMIT = WarpNumberEntityDescription(
    key="charging_current_limit",
    translation_key="charging_current_limit",
    topics=("evse/external_current", "evse/external_enabled"),
    value_fn=lambda s: s["evse/external_current"]["current"] / 1000,
    available_fn=lambda s: s["evse/external_enabled"]["enabled"] is True,
    put_path="evse/external_current",
    payload_fn=_current_payload,
    max_fn=_max_current,
    device_class=NumberDeviceClass.CURRENT,
    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    native_min_value=0,
    native_max_value=32,
    native_step=1,
    mode=NumberMode.AUTO,
)

CHARGE_ENERGY_LIMIT = WarpNumberEntityDescription(
    key="charge_energy_limit",
    translation_key="charge_energy_limit",
    topics=("charge_limits/active_limits",),
    value_fn=lambda s: s["charge_limits/active_limits"]["energy_wh"] / 1000,
    put_path="charge_limits/override_energy",
    payload_fn=lambda v: {"energy_wh": round(v * 1000)},
    device_class=NumberDeviceClass.ENERGY,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    native_min_value=0,
    native_max_value=100,
    native_step=0.5,
    mode=NumberMode.AUTO,
)

CHARGE_SOC_LIMIT = WarpNumberEntityDescription(
    key="charge_soc_limit",
    translation_key="charge_soc_limit",
    topics=("charge_limits/active_limits",),
    value_fn=lambda s: s["charge_limits/active_limits"]["soc_pct"],
    put_path="charge_limits/override_soc",
    payload_fn=lambda v: {"soc_pct": int(v)},
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=0,
    native_max_value=100,
    native_step=1,
    mode=NumberMode.AUTO,
)

VEHICLE_SOC = WarpNumberEntityDescription(
    key="vehicle_soc",
    translation_key="vehicle_soc",
    topics=("ev/state",),
    value_fn=lambda s: s["ev/state"]["soc"],
    put_path="ev/inject_soc",
    payload_fn=lambda v: {"soc": v},
    device_class=NumberDeviceClass.BATTERY,
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=0,
    native_max_value=100,
    native_step=0.1,
    mode=NumberMode.BOX,
)


def descriptions(states: States) -> list[WarpNumberEntityDescription]:
    """Number descriptions the device supports."""
    result: list[WarpNumberEntityDescription] = []
    if has_feature(states, "evse"):
        result.append(CHARGING_CURRENT_LIMIT)
        if "charge_limits/active_limits" in states:
            result.append(CHARGE_ENERGY_LIMIT)
            if "soc_pct" in (states["charge_limits/active_limits"] or {}):
                result.append(CHARGE_SOC_LIMIT)
    if "ev/state" in states:
        result.append(VEHICLE_SOC)
    return result


class WarpNumber(WarpEntity, NumberEntity):
    """Number entity; the new value arrives via the device echo."""

    entity_description: WarpNumberEntityDescription

    def __init__(self, runtime: WarpRuntime, description: WarpNumberEntityDescription) -> None:
        """Initialize the entity; the maximum is structural (hardware jumper)."""
        super().__init__(runtime, description)
        if description.max_fn is not None:
            with contextlib.suppress(VALUE_ERRORS):
                self._attr_native_max_value = description.max_fn(runtime.client.states)

    @callback
    def _update_from_value(self, value: Any) -> None:
        self._attr_native_value = value

    async def async_set_native_value(self, value: float) -> None:
        """Send the value to the device."""
        description = self.entity_description
        await self.runtime.async_put(description.put_path, description.payload_fn(value))


async def async_setup_entry(
    hass: HomeAssistant, entry: WarpConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the number entities."""
    runtime = entry.runtime_data
    async_add_entities(WarpNumber(runtime, d) for d in descriptions(runtime.client.states))
