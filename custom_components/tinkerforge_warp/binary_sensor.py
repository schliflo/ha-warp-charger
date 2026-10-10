"""Binary sensors for the Tinkerforge WARP integration."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import (
    States,
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    config_enabled,
    has_feature,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WarpBinarySensorEntityDescription(WarpEntityDescription, BinarySensorEntityDescription):
    """Describes a WARP binary sensor."""


class WarpBinarySensor(WarpEntity, BinarySensorEntity):
    """Binary sensor whose state follows its description's ``value_fn``."""

    entity_description: WarpBinarySensorEntityDescription

    @callback
    def _update_from_value(self, value: Any) -> None:
        self._attr_is_on = None if value is None else bool(value)


def descriptions(states: States) -> list[WarpBinarySensorEntityDescription]:
    """Return the binary sensors this device supports."""
    result: list[WarpBinarySensorEntityDescription] = []
    if has_feature(states, "evse"):
        result += [
            WarpBinarySensorEntityDescription(
                key="plugged",
                translation_key="plugged",
                device_class=BinarySensorDeviceClass.PLUG,
                topics=("evse/state",),
                value_fn=lambda s: s["evse/state"]["charger_state"] in (1, 2, 3),
            ),
            WarpBinarySensorEntityDescription(
                key="charging",
                translation_key="charging",
                device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
                topics=("evse/state",),
                value_fn=lambda s: s["evse/state"]["charger_state"] == 3,
            ),
            WarpBinarySensorEntityDescription(
                key="problem",
                translation_key="problem",
                device_class=BinarySensorDeviceClass.PROBLEM,
                topics=("evse/state",),
                value_fn=lambda s: (
                    s["evse/state"]["charger_state"] == 4 or s["evse/state"]["error_state"] != 0
                ),
            ),
            WarpBinarySensorEntityDescription(
                key="button_pressed",
                translation_key="button_pressed",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                topics=("evse/button_state",),
                value_fn=lambda s: s["evse/button_state"]["button_pressed"],
            ),
        ]
    if config_enabled(states, "p14a_enwg/config"):
        result.append(
            WarpBinarySensorEntityDescription(
                key="p14a_limited",
                translation_key="p14a_limited",
                topics=("p14a_enwg/state",),
                value_fn=lambda s: s["p14a_enwg/state"]["active"],
            )
        )
    return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WarpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the WARP binary sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        WarpBinarySensor(runtime, description)
        for description in descriptions(runtime.client.states)
    )
