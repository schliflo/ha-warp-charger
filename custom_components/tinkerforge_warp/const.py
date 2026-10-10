"""Constants for the Tinkerforge WARP integration."""

from homeassistant.const import Platform

DOMAIN = "tinkerforge_warp"

CONF_URL = "url"

GRACE_PERIOD = 30  # seconds a disconnect is hidden from entities
RELOAD_DEBOUNCE = 15  # seconds to wait before reloading on structure changes

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]
