"""Config flow tests against the fake WARP device."""

from collections.abc import Generator
from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.tinkerforge_warp.config_flow import CannotConnect
from custom_components.tinkerforge_warp.const import CONF_URL, DOMAIN
from tests.conftest import FakeWarpFactory
from tests.fake_warp import REMOVE

ADVANCED = {CONF_SSL: False, CONF_VERIFY_SSL: False}
AUTH = ("admin", "secret")


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Don't set up the integration (and don't open a WebSocket) from flow tests."""
    with patch("custom_components.tinkerforge_warp.async_setup_entry", return_value=True) as mock:
        yield mock


def _host(base_url: str) -> str:
    return URL(base_url).authority


def _user_input(base_url: str, **extra: object) -> dict:
    return {CONF_HOST: _host(base_url), "advanced": ADVANCED, **extra}


def _zeroconf(base_url: str, uid: str = "257899", **props: str) -> ZeroconfServiceInfo:
    url = URL(base_url)
    return ZeroconfServiceInfo(
        ip_address=ip_address(url.host),
        ip_addresses=[ip_address(url.host)],
        port=url.port,
        hostname="warp3-2jex.local.",
        type="_http._tcp.local.",
        name="warp3-2jEx._http._tcp.local.",
        properties={"brand": "tinkerforge", "model": "warp3", "uid": uid, **props},
    )


def _entry(url: str, **data: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="2jEx",
        title="warp3-charger-pro",
        data={CONF_URL: url, CONF_VERIFY_SSL: False, **data},
    )


async def test_user_flow(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The user step creates an entry keyed by the device uid."""
    fake = await fake_warp("warp3")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake.base_url)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "warp3-charger-pro"
    assert result["data"] == {CONF_URL: fake.base_url, CONF_VERIFY_SSL: False}
    assert result["result"].unique_id == "2jEx"


async def test_user_flow_title_fallback(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Without info/display_name the device name is used."""
    fake = await fake_warp("warp3", patches={"info/display_name": REMOVE})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake.base_url)
    )
    assert result["title"] == "warp3-2jEx"


async def test_user_flow_invalid_auth_then_success(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A 401 shows invalid_auth; credentials then succeed and are stored."""
    fake = await fake_warp("warp3", auth=AUTH)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake.base_url)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _user_input(fake.base_url, **{CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}),
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _user_input(fake.base_url, **{CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: fake.base_url,
        CONF_VERIFY_SSL: False,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "secret",
    }


async def test_user_flow_cannot_connect(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unreachable host -> cannot_connect, recoverable."""
    fake = await fake_warp("warp3")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1:1", "advanced": ADVANCED}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake.base_url)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "patches",
    [
        {"info/name": {"uid": None}},
        {"info/name": []},
        {"info/name": REMOVE},
    ],
)
async def test_user_flow_not_a_warp(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, patches: dict
) -> None:
    """A reply without a usable uid is treated as unreachable."""
    fake = await fake_warp("warp3", patches=patches)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake.base_url)
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unexpected exceptions are reported as unknown."""
    fake = await fake_warp("warp3")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch(
        "custom_components.tinkerforge_warp.config_flow.async_validate",
        side_effect=RuntimeError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _user_input(fake.base_url)
        )
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """The same device cannot be added twice."""
    fake = await fake_warp("warp3")
    _entry(fake.base_url).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake.base_url)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_stores_final_url_after_redirect(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """The URL after redirects (scheme/port) is what gets stored."""
    target = await fake_warp("warp3")
    redirecting = await fake_warp("warp3", redirect_to=target.base_url)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(redirecting.base_url)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == target.base_url


# ---- zeroconf ----


async def test_zeroconf_new_device(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Discovery maps the decimal uid to the base58 uid and confirms without credentials."""
    fake = await fake_warp("warp3")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=_zeroconf(fake.base_url)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["data_schema"] is None
    assert result["description_placeholders"] == {"name": "warp3-2jEx"}
    assert hass.config_entries.flow.async_get(result["flow_id"])["context"][
        "title_placeholders"
    ] == {"name": "warp3-2jEx"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "warp3-charger-pro"
    assert result["data"] == {CONF_URL: fake.base_url, CONF_VERIFY_SSL: False}
    assert result["result"].unique_id == "2jEx"


async def test_zeroconf_existing_entry_keeps_scheme_and_port(hass: HomeAssistant) -> None:
    """Only the host of a known device is updated; https is not downgraded."""
    entry = _entry("https://192.0.2.1:8443")
    entry.add_to_hass(hass)
    info = _zeroconf("http://127.0.0.7:80")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_URL] == "https://127.0.0.7:8443"


async def test_zeroconf_invalid_uid(hass: HomeAssistant) -> None:
    """A malformed uid TXT record aborts."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf("http://127.0.0.7:80", uid="abc"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery"


async def test_zeroconf_default_port_omitted(hass: HomeAssistant) -> None:
    """Port 80 is not stored in the URL; an unreachable device aborts."""
    with patch(
        "custom_components.tinkerforge_warp.config_flow.async_validate",
        side_effect=CannotConnect,
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf("http://127.0.0.1:80")
        )
    assert validate.call_args.args[1] == "http://127.0.0.1"
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_zeroconf_requires_auth(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Credentials are asked for only when the device wants them."""
    fake = await fake_warp("warp3", auth=AUTH)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=_zeroconf(fake.base_url)
    )
    assert result["step_id"] == "zeroconf_confirm"
    assert result["data_schema"] is not None
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: fake.base_url,
        CONF_VERIFY_SSL: False,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "secret",
    }


async def test_zeroconf_confirm_cannot_connect(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """The device vanishing between discovery and confirmation shows an error."""
    fake = await fake_warp("warp3")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=_zeroconf(fake.base_url)
    )
    await fake.close()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# ---- reauth ----


async def test_reauth(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Reauth validates new credentials and updates the entry."""
    fake = await fake_warp("warp3", auth=AUTH)
    entry = _entry(fake.base_url, **{CONF_USERNAME: "admin", CONF_PASSWORD: "old"})
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "old"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "secret"
    assert entry.data[CONF_URL] == fake.base_url


async def test_reauth_cannot_connect(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """An unreachable device keeps the reauth form open."""
    fake = await fake_warp("warp3", auth=AUTH)
    entry = _entry(fake.base_url)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    await fake.close()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_wrong_device(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Credentials for a different charger are rejected."""
    fake = await fake_warp("warp3", patches={"info/name": {"uid": "other"}})
    entry = _entry(fake.base_url)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"


# ---- reconfigure ----


async def test_reconfigure(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Reconfigure is prefilled from the entry and replaces its data."""
    old = await fake_warp("warp3")
    new = await fake_warp("warp3", auth=AUTH)
    entry = _entry(old.base_url)
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested[CONF_HOST] == _host(old.base_url)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _user_input(new.base_url, **{CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_URL: new.base_url,
        CONF_VERIFY_SSL: False,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "secret",
    }


async def test_reconfigure_wrong_device(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Pointing the entry at another charger is rejected."""
    old = await fake_warp("warp3")
    other = await fake_warp("warp3", patches={"info/name": {"uid": "other"}})
    entry = _entry(old.base_url, **{CONF_USERNAME: "u", CONF_PASSWORD: "p"})
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(other.base_url)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_URL] == old.base_url
