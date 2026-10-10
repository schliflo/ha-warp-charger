"""Runtime state and base entity for the Tinkerforge WARP integration."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.event import async_call_later

from warp_charger import WarpAuthError, WarpClient, WarpCommandError, WarpConnectionError

from .const import DOMAIN, GRACE_PERIOD

_LOGGER = logging.getLogger(__name__)

type WarpConfigEntry = ConfigEntry[WarpRuntime]
type States = Mapping[str, Any]

# Firmware drift: missing keys / odd shapes make a value unknown instead of raising.
VALUE_ERRORS = (KeyError, TypeError, IndexError, ValueError, ZeroDivisionError)


class WarpRuntime:
    """Per-entry runtime: client, identity, availability with grace period, device infos."""

    def __init__(self, hass: HomeAssistant, entry: WarpConfigEntry, client: WarpClient) -> None:
        """Initialize the runtime; the client must already have received its first dump."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.uid: str = client.states["info/name"]["uid"]
        self.available = True
        self.main_device_id: str | None = None  # set by setup once the device is registered
        self._available_subs: list[Callable[[], None]] = []
        self._grace_unsub: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Start tracking the connection; cleaned up on entry unload."""
        self.entry.async_on_unload(self.client.subscribe_connection(self._on_connection))
        self.entry.async_on_unload(self._cancel_grace)

    @callback
    def _cancel_grace(self) -> None:
        if self._grace_unsub is not None:
            self._grace_unsub()
            self._grace_unsub = None

    @callback
    def _on_connection(self, connected: bool) -> None:
        if connected:
            self._cancel_grace()
            self._set_available(True)
        elif self._grace_unsub is None:
            self._grace_unsub = async_call_later(self.hass, GRACE_PERIOD, self._grace_expired)

    @callback
    def _grace_expired(self, _now: datetime) -> None:
        self._grace_unsub = None
        self._set_available(False)

    def _set_available(self, available: bool) -> None:
        if self.available == available:
            return
        self.available = available
        for cb in list(self._available_subs):
            cb()

    @callback
    def subscribe_available(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call ``cb()`` when availability changes."""
        self._available_subs.append(cb)

        def unsubscribe() -> None:
            if cb in self._available_subs:
                self._available_subs.remove(cb)

        return unsubscribe

    def device_info(self, device: str | None = None) -> DeviceInfo:
        """Device info for the main device (None) or a meter sub-device (``meter_N``)."""
        states = self.client.states
        if device is not None:
            index = device.removeprefix("meter_")
            try:
                name = states[f"meters/{index}/config"][1]["display_name"] or None
            except VALUE_ERRORS:
                name = None
            sub_info = DeviceInfo(
                identifiers={(DOMAIN, f"{self.uid}_{device}")},
                name=name or f"Meter {index}",
                manufacturer="Tinkerforge",
            )
            if self.main_device_id is not None:
                sub_info["via_device_id"] = self.main_device_id
            return sub_info

        info = DeviceInfo(
            identifiers={(DOMAIN, self.uid)},
            manufacturer="Tinkerforge",
            configuration_url=self.client.base_url,
        )
        name = states.get("info/name") or {}
        info["model"] = name.get("display_type")
        info["model_id"] = name.get("type")
        info["name"] = (states.get("info/display_name") or {}).get("display_name") or name.get(
            "name"
        )
        info["sw_version"] = (states.get("info/version") or {}).get("firmware")
        try:
            evse_version = states["evse/hardware_configuration"]["evse_version"]
            info["hw_version"] = f"EVSE {evse_version / 10:.1f}"
        except VALUE_ERRORS:
            pass
        for topic, key in (("ethernet/state", "mac"), ("wifi/state", "sta_mac")):
            if mac := (states.get(topic) or {}).get(key):
                info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
                break
        return info

    async def async_put(self, path: str, payload: Any = None) -> None:
        """PUT to the device, translating failures into ``HomeAssistantError``."""
        try:
            await self.client.put(path, payload)
        except WarpCommandError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except WarpConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except WarpAuthError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err


@dataclass(frozen=True, kw_only=True)
class WarpEntityDescription(EntityDescription):
    """Describes a WARP entity derived from one or more state topics."""

    topics: tuple[str, ...]
    value_fn: Callable[[States], Any]
    available_fn: Callable[[States], bool] | None = None
    device: str | None = None


class WarpEntity(Entity):
    """Push-based entity: re-evaluates when one of its topics arrives."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: WarpEntityDescription

    def __init__(self, runtime: WarpRuntime, description: WarpEntityDescription) -> None:
        """Initialize the entity."""
        self.runtime = runtime
        self.entity_description = description
        self.warp_value: Any = None
        self._initialized = False
        self._available = runtime.available
        self._attr_unique_id = f"{runtime.uid}_{description.key}"
        self._attr_device_info = runtime.device_info(description.device)

    @property
    def available(self) -> bool:
        """Return True if the charger is reachable and the extra condition holds."""
        return self._available

    @callback
    def _update_from_value(self, value: Any) -> None:
        """Set ``_attr_*`` from the new value."""

    @callback
    def _recompute(self) -> bool:
        """Recompute value and availability; return True if either changed."""
        states = self.runtime.client.states
        description = self.entity_description
        try:
            value = description.value_fn(states)
        except VALUE_ERRORS:
            value = None
        available = self.runtime.available
        if available and description.available_fn is not None:
            try:
                available = bool(description.available_fn(states))
            except VALUE_ERRORS:
                available = False
        changed = not self._initialized or value != self.warp_value or available != self._available
        self._initialized = True
        self.warp_value = value
        self._available = available
        if changed:
            self._update_from_value(value)
        return changed

    @callback
    def _handle_update(self, *_args: Any) -> None:
        if self._recompute():
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Compute the initial value and subscribe to topics and availability."""
        self._recompute()
        client = self.runtime.client
        for topic in self.entity_description.topics:
            self.async_on_remove(client.subscribe(topic, self._handle_update))
        self.async_on_remove(self.runtime.subscribe_available(self._handle_update))


def has_feature(states: States, feature: str) -> bool:
    """Return True if ``info/features`` lists ``feature``."""
    return feature in (states.get("info/features") or ())


def evse_version(states: States) -> int:
    """EVSE hardware version (14/15 WARP1, 20 WARP2, 30 WARP3/4); 0 without EVSE."""
    try:
        return int(states["evse/hardware_configuration"]["evse_version"])
    except VALUE_ERRORS:
        return 0


def config_enabled(states: States, topic: str, key: str = "enable") -> bool:
    """Return True if ``states[topic][key]`` is truthy (missing -> False)."""
    try:
        return bool(states[topic][key])
    except VALUE_ERRORS:
        return False
