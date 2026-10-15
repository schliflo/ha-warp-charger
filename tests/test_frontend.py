"""Lovelace card registration tests."""

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.tinkerforge_warp.const import DOMAIN
from custom_components.tinkerforge_warp.frontend import CARD_URL, async_register_card


async def test_card_registered(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, socket_enabled: None
) -> None:
    """With the frontend loaded, the card is served and added as an extra module."""
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])

    await async_register_card(hass)

    version = (await async_get_integration(hass, DOMAIN)).version
    assert hass.data[DATA_EXTRA_MODULE_URL].urls == {f"{CARD_URL}?v={version}"}
    response = await (await hass_client()).get(CARD_URL)
    assert response.status == 200
    assert "customElements.define" in await response.text()


async def test_card_skipped_without_frontend(hass: HomeAssistant) -> None:
    """Without the frontend, setup does not fail and nothing is registered."""
    assert await async_setup_component(hass, DOMAIN, {})
    assert DATA_EXTRA_MODULE_URL not in hass.data
