"""Event entities for the Tinkerforge WARP integration."""

import logging
from dataclasses import dataclass

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.components.tag import async_scan_tag
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from warp_charger.nfc import NfcScanDetector

from .entity import (
    VALUE_ERRORS,
    States,
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    WarpRuntime,
    has_feature,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WarpEventEntityDescription(WarpEntityDescription, EventEntityDescription):
    """Describes a WARP event entity."""


class WarpNfcTagEvent(WarpEntity, EventEntity):
    """Fires ``scanned`` for every new NFC tag scan and forwards it to HA's tag registry."""

    entity_description: WarpEventEntityDescription

    def __init__(self, runtime: WarpRuntime, description: WarpEventEntityDescription) -> None:
        """Initialize the entity."""
        super().__init__(runtime, description)
        self._detector = NfcScanDetector()

    @callback
    def _recompute(self) -> bool:
        """Fire events for new scans; state is only written for availability changes."""
        available = self.runtime.available
        changed = not self._initialized or available != self._available
        self._initialized = True
        self._available = available
        try:
            scans = self._detector.feed(self.runtime.client.states["nfc/seen_tags"])
        except VALUE_ERRORS:
            scans = []
        for tag_type, tag_id in reversed(scans):  # oldest first
            self._trigger_event("scanned", {"tag_id": tag_id, "tag_type": tag_type})
            self.async_write_ha_state()
            self.hass.async_create_task(self._async_scan_tag(tag_id))
        return changed

    async def _async_scan_tag(self, tag_id: str) -> None:
        try:
            await async_scan_tag(
                self.hass, tag_id, self.device_entry.id if self.device_entry else None
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Could not report scanned tag: %s", err)


def descriptions(states: States) -> list[WarpEventEntityDescription]:
    """Return the event entities this device supports."""
    if not has_feature(states, "nfc"):
        return []
    return [
        WarpEventEntityDescription(
            key="nfc_tag",
            translation_key="nfc_tag",
            event_types=["scanned"],
            topics=("nfc/seen_tags",),
            value_fn=lambda s: s["nfc/seen_tags"],
        )
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WarpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the WARP event entities."""
    runtime = entry.runtime_data
    async_add_entities(
        WarpNfcTagEvent(runtime, description) for description in descriptions(runtime.client.states)
    )
