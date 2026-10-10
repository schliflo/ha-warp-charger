"""Diagnostics tests."""

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tinkerforge_warp.const import CONF_URL, DOMAIN
from custom_components.tinkerforge_warp.diagnostics import async_get_config_entry_diagnostics
from tests.conftest import FakeWarpFactory

REDACTED = "**REDACTED**"


async def test_diagnostics(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Consumed topics and info/* are included; sensitive keys are redacted."""
    fake = await fake_warp("warp3", auth=("admin", "secret"))
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2jEx",
        data={
            CONF_URL: fake.base_url,
            CONF_VERIFY_SSL: False,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)
    consumer = SimpleNamespace(
        descriptions=lambda _states: [
            SimpleNamespace(
                key="x", device=None, topics=("ethernet/state", "wifi/state", "evse/state")
            )
        ]
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch(
        "custom_components.tinkerforge_warp.diagnostics.platform_modules",
        lambda: {Platform.SENSOR: consumer},
    ):
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert diagnostics["entry"] == {
        CONF_URL: REDACTED,
        CONF_VERIFY_SSL: False,
        CONF_USERNAME: REDACTED,
        CONF_PASSWORD: REDACTED,
    }
    assert diagnostics["connected"] is True
    assert diagnostics["kicked_repeatedly"] is False

    states = diagnostics["states"]
    assert states["info/name"]["uid"] == "2jEx"
    assert states["info/version"]["firmware"] == "2.12.1+6a4f9137"
    assert states["info/display_name"] == {"display_name": REDACTED}
    assert states["ethernet/state"]["mac"] == REDACTED
    assert states["ethernet/state"]["ip"] == REDACTED
    assert states["ethernet/state"]["link_speed"] == 100
    assert states["wifi/state"]["sta_mac"] == REDACTED
    assert states["wifi/state"]["ap_bssid"] == REDACTED
    assert "evse/state" in states
    # Gating topics are included (redacted), other unconsumed topics are left out.
    assert states["meters/0/value_ids"][0] == 1
    assert states["wifi/sta_config"]["passphrase"] in ("", REDACTED)
    assert "mqtt/config" not in states
    assert "users/config" not in states
    assert "192.0.2.1" not in str(diagnostics)
