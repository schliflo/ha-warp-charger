"""Config flow for the Tinkerforge WARP integration."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
import probatio
from aiohttp import DigestAuthMiddleware
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from yarl import URL

from warp_charger import uid as warp_uid

from .const import CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_ADVANCED = "advanced"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

CREDENTIALS_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_USERNAME): str,
        probatio.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)

CONNECTION_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Optional(CONF_USERNAME): str,
        probatio.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
        probatio.Required(CONF_ADVANCED): section(
            probatio.Schema(
                {
                    probatio.Optional(CONF_SSL, default=False): bool,
                    probatio.Optional(CONF_VERIFY_SSL, default=False): bool,
                }
            ),
            {"collapsed": True},
        ),
    }
)


class CannotConnect(Exception):
    """The device could not be reached or answered unexpectedly."""


class InvalidAuth(Exception):
    """The device rejected the credentials."""


@dataclass(frozen=True)
class DeviceInfo:
    """What validation learned about the device."""

    uid: str
    title: str
    url: str


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    middlewares: tuple[DigestAuthMiddleware, ...],
) -> tuple[Any, URL]:
    """GET ``url``; return the JSON body and the final URL (after redirects)."""
    kwargs: dict[str, Any] = {"middlewares": middlewares} if middlewares else {}
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT, **kwargs) as resp:
            if resp.status == 401:
                raise InvalidAuth
            resp.raise_for_status()
            return await resp.json(content_type=None), resp.url
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise CannotConnect from err


async def async_validate(
    hass: HomeAssistant,
    url: str,
    username: str | None,
    password: str | None,
    verify_ssl: bool,
) -> DeviceInfo:
    """Check that ``url`` is a (reachable, authenticated) WARP device."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    # A fresh middleware per flow step: digest state must not leak between attempts.
    middlewares = (DigestAuthMiddleware(username, password or ""),) if username else ()

    data, final_url = await _get_json(session, f"{url.rstrip('/')}/info/name", middlewares)
    try:
        device_uid = data["uid"]
    except (KeyError, TypeError) as err:
        raise CannotConnect from err
    if not isinstance(device_uid, str) or not device_uid:
        raise CannotConnect

    base_url = str(final_url.origin())
    title: str | None = None
    try:
        display, _ = await _get_json(session, f"{base_url}/info/display_name", middlewares)
        title = display["display_name"]
    except CannotConnect, InvalidAuth, KeyError, TypeError:
        pass
    return DeviceInfo(device_uid, title or data.get("name") or device_uid, base_url)


def _build_url(host: str, ssl: bool) -> str:
    return f"{'https' if ssl else 'http'}://{host.strip().rstrip('/')}"


def _entry_data(
    info: DeviceInfo, username: str | None, password: str | None, verify_ssl: bool
) -> dict[str, Any]:
    data: dict[str, Any] = {CONF_URL: info.url, CONF_VERIFY_SSL: verify_ssl}
    if username:
        data[CONF_USERNAME] = username
        data[CONF_PASSWORD] = password or ""
    return data


class WarpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tinkerforge WARP."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_url: str | None = None
        self._needs_auth = False

    # ---- user / reconfigure ----

    async def _async_connection_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Shared host/credentials form of the user and reconfigure steps."""
        errors: dict[str, str] = {}
        suggested: Mapping[str, Any] | None = user_input
        if user_input is not None:
            advanced = user_input[CONF_ADVANCED]
            username = user_input.get(CONF_USERNAME) or None
            password = user_input.get(CONF_PASSWORD)
            verify_ssl = advanced[CONF_VERIFY_SSL]
            try:
                info = await async_validate(
                    self.hass,
                    _build_url(user_input[CONF_HOST], advanced[CONF_SSL]),
                    username,
                    password,
                    verify_ssl,
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info.uid)
                data = _entry_data(info, username, password, verify_ssl)
                if self.source == SOURCE_RECONFIGURE:
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data=data
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info.title, data=data)
        elif self.source == SOURCE_RECONFIGURE:
            suggested = self._reconfigure_suggested_values()

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(CONNECTION_SCHEMA, suggested),
            errors=errors,
        )

    def _reconfigure_suggested_values(self) -> dict[str, Any]:
        entry = self._get_reconfigure_entry()
        url = URL(entry.data[CONF_URL])
        return {
            CONF_HOST: url.host_port_subcomponent,
            CONF_USERNAME: entry.data.get(CONF_USERNAME),
            CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
            CONF_ADVANCED: {
                CONF_SSL: url.scheme == "https",
                CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, False),
            },
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual setup."""
        return await self._async_connection_step("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle changing host or credentials."""
        return await self._async_connection_step("reconfigure", user_input)

    # ---- zeroconf ----

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle mDNS discovery."""
        try:
            unique_id = warp_uid.encode(int(discovery_info.properties["uid"]))
        except KeyError, ValueError:
            return self.async_abort(reason="invalid_discovery")

        await self.async_set_unique_id(unique_id)
        if entry := self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id):
            # Only the host changes: keep scheme and port (an https entry stays https).
            new_url = str(URL(entry.data[CONF_URL]).with_host(discovery_info.host)).rstrip("/")
            self._abort_if_unique_id_configured(updates={CONF_URL: new_url})

        port = discovery_info.port
        self._discovered_url = str(
            URL.build(
                scheme="http", host=discovery_info.host, port=None if port in (None, 80) else port
            )
        ).rstrip("/")
        self.context["title_placeholders"] = {"name": discovery_info.name.split(".")[0]}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device, asking for credentials only if required."""
        assert self._discovered_url is not None
        placeholders = {"name": self.context["title_placeholders"]["name"]}
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                await async_validate(self.hass, self._discovered_url, None, None, False)
            except CannotConnect:
                return self.async_abort(reason="cannot_connect")
            except InvalidAuth:
                self._needs_auth = True
        else:
            username = user_input.get(CONF_USERNAME) or None
            password = user_input.get(CONF_PASSWORD)
            try:
                info = await async_validate(
                    self.hass, self._discovered_url, username, password, False
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                self._needs_auth = True
                errors["base"] = "invalid_auth"
            else:
                return self.async_create_entry(
                    title=info.title, data=_entry_data(info, username, password, False)
                )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=CREDENTIALS_SCHEMA if self._needs_auth else None,
            description_placeholders=placeholders,
            errors=errors,
        )

    # ---- reauth ----

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input.get(CONF_USERNAME) or None
            password = user_input.get(CONF_PASSWORD)
            verify_ssl = entry.data.get(CONF_VERIFY_SSL, False)
            try:
                info = await async_validate(
                    self.hass, entry.data[CONF_URL], username, password, verify_ssl
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(info.uid)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry, data=_entry_data(info, username, password, verify_ssl)
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input or {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            description_placeholders={"name": entry.title},
            errors=errors,
        )
