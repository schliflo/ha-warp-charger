"""Switch platform: charger configuration toggles."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import (
    States,
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    evse_version,
    has_feature,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WarpSwitchEntityDescription(WarpEntityDescription, SwitchEntityDescription):
    """Switch description: the topic's single member is written back via ``<topic>_update``."""

    topic: str
    member: str
    min_evse_version: int = 0


def _switch(
    key: str, topic: str, member: str, *, enabled: bool = True, min_evse_version: int = 0
) -> WarpSwitchEntityDescription:
    return WarpSwitchEntityDescription(
        key=key,
        translation_key=key,
        topics=(topic,),
        value_fn=lambda s: s[topic][member],
        topic=topic,
        member=member,
        min_evse_version=min_evse_version,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=enabled,
    )


SWITCHES = (
    _switch("auto_start_charging", "evse/auto_start_charging", "auto_start_charging"),
    _switch(
        "external_clear_on_disconnect",
        "evse/external_clear_on_disconnect",
        "clear_on_disconnect",
        enabled=False,
    ),
    _switch("boost_mode", "evse/boost_mode", "enabled", enabled=False),
    _switch("phase_auto_switch", "evse/phase_auto_switch", "enabled", min_evse_version=30),
)


def descriptions(states: States) -> list[WarpSwitchEntityDescription]:
    """Switch descriptions the device supports."""
    if not has_feature(states, "evse"):
        return []
    version = evse_version(states)
    return [d for d in SWITCHES if d.topic in states and version >= d.min_evse_version]


class WarpSwitch(WarpEntity, SwitchEntity):
    """Switch entity; the new state arrives via the device echo."""

    entity_description: WarpSwitchEntityDescription

    @callback
    def _update_from_value(self, value: Any) -> None:
        self._attr_is_on = None if value is None else bool(value)

    async def _set(self, on: bool) -> None:
        description = self.entity_description
        await self.runtime.async_put(f"{description.topic}_update", {description.member: on})

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._set(False)


async def async_setup_entry(
    hass: HomeAssistant, entry: WarpConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the switch entities."""
    runtime = entry.runtime_data
    async_add_entities(WarpSwitch(runtime, d) for d in descriptions(runtime.client.states))
