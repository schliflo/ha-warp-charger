"""Select platform: charge mode, phases and charge duration limit."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from warp_charger.const import CHARGE_MODE, CHARGE_MODE_DEFAULT, DURATION_LIMITS

from .entity import (
    VALUE_ERRORS,
    States,
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    WarpRuntime,
    config_enabled,
    has_feature,
)

PARALLEL_UPDATES = 0

PHASES = {1: "single_phase", 3: "three_phase"}
_CHARGE_MODE_BY_OPTION = {name: mode for mode, name in CHARGE_MODE.items()}
_DURATION_BY_OPTION = {name: n for n, name in DURATION_LIMITS.items()}
_PHASES_BY_OPTION = {name: n for n, name in PHASES.items()}


@dataclass(frozen=True, kw_only=True)
class WarpSelectEntityDescription(WarpEntityDescription, SelectEntityDescription):
    """Select description with the command it sends.

    ``dynamic_options``: ``value_fn`` returns ``(current_option, options)``.
    """

    put_path: str
    payload_fn: Callable[[str], Any]
    dynamic_options: bool = False


def _charge_mode(
    mode_topic: str, supported_topic: str, put_path: str
) -> WarpSelectEntityDescription:
    def value_fn(states: States) -> tuple[str | None, tuple[str, ...]]:
        options = tuple(
            CHARGE_MODE[m]
            for m in sorted(set(states[supported_topic]))
            if m != CHARGE_MODE_DEFAULT and m in CHARGE_MODE
        )
        current = CHARGE_MODE.get(states[mode_topic]["mode"])
        return (current if current in options else None, options)

    return WarpSelectEntityDescription(
        key="charge_mode",
        translation_key="charge_mode",
        topics=(mode_topic, supported_topic),
        value_fn=value_fn,
        put_path=put_path,
        payload_fn=lambda option: {"mode": _CHARGE_MODE_BY_OPTION[option]},
        dynamic_options=True,
    )


CHARGE_MODE_MANAGER = _charge_mode(
    "power_manager/charge_mode",
    "charge_manager/supported_charge_modes",
    "power_manager/charge_mode_update",
)
CHARGE_MODE_EVSE = _charge_mode(
    "evse/charge_mode", "evse/supported_charge_modes", "evse/charge_mode_update"
)

PHASES_SELECT = WarpSelectEntityDescription(
    key="phases",
    translation_key="phases",
    topics=("power_manager/external_control", "power_manager/state"),
    value_fn=lambda s: PHASES.get(s["power_manager/external_control"]["phases_wanted"]),
    available_fn=lambda s: s["power_manager/state"]["external_control"] == 0,
    options=list(PHASES.values()),
    put_path="power_manager/external_control_update",
    payload_fn=lambda option: {"phases_wanted": _PHASES_BY_OPTION[option]},
)

CHARGE_DURATION_LIMIT = WarpSelectEntityDescription(
    key="charge_duration_limit",
    translation_key="charge_duration_limit",
    topics=("charge_limits/active_limits",),
    value_fn=lambda s: DURATION_LIMITS.get(s["charge_limits/active_limits"]["duration"]),
    options=list(DURATION_LIMITS.values()),
    put_path="charge_limits/override_duration",
    payload_fn=lambda option: {"duration": _DURATION_BY_OPTION[option]},
)


def descriptions(states: States) -> list[WarpSelectEntityDescription]:
    """Select descriptions the device supports."""
    result: list[WarpSelectEntityDescription] = []
    if config_enabled(states, "charge_manager/config", "enable_charge_manager"):
        result.append(CHARGE_MODE_MANAGER)
    elif config_enabled(states, "evse/management_enabled", "enabled"):
        result.append(CHARGE_MODE_EVSE)
    if has_feature(states, "phase_switch"):
        result.append(PHASES_SELECT)
    if has_feature(states, "evse") and "charge_limits/active_limits" in states:
        result.append(CHARGE_DURATION_LIMIT)
    return result


class WarpSelect(WarpEntity, SelectEntity):
    """Select entity; the new option arrives via the device echo."""

    entity_description: WarpSelectEntityDescription

    def __init__(self, runtime: WarpRuntime, description: WarpSelectEntityDescription) -> None:
        """Initialize the entity; dynamic options must exist before it is added."""
        super().__init__(runtime, description)
        if description.dynamic_options:
            self._update_from_value(self._safe_value())

    def _safe_value(self) -> Any:
        try:
            return self.entity_description.value_fn(self.runtime.client.states)
        except VALUE_ERRORS:
            return None

    @callback
    def _update_from_value(self, value: Any) -> None:
        if self.entity_description.dynamic_options:
            current, options = value if value is not None else (None, ())
            self._attr_options = list(options)
            self._attr_current_option = current
        else:
            self._attr_current_option = value

    async def async_select_option(self, option: str) -> None:
        """Send the option to the device."""
        description = self.entity_description
        await self.runtime.async_put(description.put_path, description.payload_fn(option))


async def async_setup_entry(
    hass: HomeAssistant, entry: WarpConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the select entities."""
    runtime = entry.runtime_data
    async_add_entities(WarpSelect(runtime, d) for d in descriptions(runtime.client.states))
