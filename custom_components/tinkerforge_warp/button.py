"""Button platform: start/stop charging, restart charge limits, reboot."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import States, WarpConfigEntry, WarpEntity, WarpEntityDescription, has_feature

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WarpButtonEntityDescription(WarpEntityDescription, ButtonEntityDescription):
    """Button description with the command it sends."""

    put_path: str
    requires_evse: bool = True
    requires_topic: str | None = None


def _button(key: str, put_path: str, **kwargs: Any) -> WarpButtonEntityDescription:
    return WarpButtonEntityDescription(
        key=key,
        translation_key=key,
        topics=(),
        value_fn=lambda _s: None,
        put_path=put_path,
        **kwargs,
    )


BUTTONS = (
    _button("start_charging", "evse/start_charging"),
    _button("stop_charging", "evse/stop_charging"),
    _button(
        "restart_charge_limits",
        "charge_limits/restart",
        requires_topic="charge_limits/active_limits",
    ),
    _button(
        "reboot",
        "reboot",
        requires_evse=False,
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def descriptions(states: States) -> list[WarpButtonEntityDescription]:
    """Button descriptions the device supports."""
    evse = has_feature(states, "evse")
    return [
        d
        for d in BUTTONS
        if (evse or not d.requires_evse)
        and (d.requires_topic is None or d.requires_topic in states)
    ]


class WarpButton(WarpEntity, ButtonEntity):
    """Button entity sending a null-body command."""

    entity_description: WarpButtonEntityDescription

    async def async_press(self) -> None:
        """Send the command."""
        await self.runtime.async_put(self.entity_description.put_path, None)


async def async_setup_entry(
    hass: HomeAssistant, entry: WarpConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the button entities."""
    runtime = entry.runtime_data
    async_add_entities(WarpButton(runtime, d) for d in descriptions(runtime.client.states))
