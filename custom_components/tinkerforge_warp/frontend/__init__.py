"""Serve the WARP charger Lovelace card and load it on every dashboard."""

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from ..const import DOMAIN

CARD_FILE = Path(__file__).parent / "warp-charger-card.js"
CARD_URL = f"/{DOMAIN}/warp-charger-card.js"


async def async_register_card(hass: HomeAssistant) -> None:
    """Register the card; no-op without the frontend (e.g. minimal setups)."""
    if "frontend" not in hass.config.components or getattr(hass, "http", None) is None:
        return
    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=True)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
