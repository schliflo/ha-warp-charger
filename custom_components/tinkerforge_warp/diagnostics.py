"""Diagnostics for the Tinkerforge WARP integration."""

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import platform_modules
from .const import CONF_URL
from .entity import WarpConfigEntry

# Topics that decide which entities exist (not consumed by entities themselves).
GATING_TOPICS = {
    "evse/hardware_configuration",
    "evse/meter_config",
    "evse/management_enabled",
    "evse/external_enabled",
    "charge_manager/config",
    "power_manager/config",
    "power_manager/state",
    "p14a_enwg/config",
    "day_ahead_prices/config",
    "solar_forecast/config",
    "wifi/sta_config",
    "firmware_update/state",
}
METER_STRUCTURE_RE = re.compile(r"^meters/\d+/(config|value_ids)$")

TO_REDACT_ENTRY = {CONF_PASSWORD, CONF_USERNAME, CONF_URL}
TO_REDACT = {
    "passphrase",
    "host",
    "gateway",
    "dns",
    "dns2",
    "place",
    "tag_id",
    "mac",
    "sta_mac",
    "ssid",
    "ip",
    "sta_ip",
    "username",
    "display_name",
    "lat",
    "long",
    "password",
    "bssid",
    "ap_bssid",
    "sta_bssid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WarpConfigEntry
) -> dict[str, Any]:
    """Return diagnostics: consumed topics and info/*, redacted."""
    client = entry.runtime_data.client
    states = client.states
    topics = {
        topic
        for module in platform_modules().values()
        for description in module.descriptions(states)
        for topic in description.topics
    }
    return {
        "entry": async_redact_data(entry.data, TO_REDACT_ENTRY),
        "connected": client.connected,
        "kicked_repeatedly": client.kicked_repeatedly,
        "states": async_redact_data(
            {
                topic: payload
                for topic, payload in states.items()
                if topic in topics
                or topic in GATING_TOPICS
                or topic.startswith("info/")
                or METER_STRUCTURE_RE.match(topic)
            },
            TO_REDACT,
        ),
    }
