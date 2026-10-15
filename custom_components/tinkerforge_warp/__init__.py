"""The Tinkerforge WARP integration."""

import logging
import re
from datetime import datetime
from types import ModuleType
from typing import Any

from aiohttp import DigestAuthMiddleware
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType

from warp_charger import WarpAuthError, WarpClient, WarpConnectionError

from . import binary_sensor, button, event, number, select, sensor, switch, update
from .const import CONF_URL, DOMAIN, PLATFORMS, RELOAD_DEBOUNCE
from .entity import States, WarpConfigEntry, WarpRuntime
from .frontend import async_register_card

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

READY_TIMEOUT = 30

STRUCTURE_TOPICS = frozenset(
    {
        "info/features",
        "info/version",
        "evse/management_enabled",
        "evse/hardware_configuration",
        "evse/meter_config",
    }
)
_STRUCTURE_RE = re.compile(r"^meters/\d+/value_ids$")

_PLATFORM_MODULES: dict[Platform, ModuleType] = {
    Platform.BINARY_SENSOR: binary_sensor,
    Platform.BUTTON: button,
    Platform.EVENT: event,
    Platform.NUMBER: number,
    Platform.SELECT: select,
    Platform.SENSOR: sensor,
    Platform.SWITCH: switch,
    Platform.UPDATE: update,
}

type Structure = frozenset[tuple[str, str, str | None, tuple[str, ...]]]


def platform_modules() -> dict[Platform, ModuleType]:
    """Return the platform modules of the enabled platforms."""
    return {platform: _PLATFORM_MODULES[platform] for platform in PLATFORMS}


def structure(states: States) -> Structure:
    """Return {(platform, key, device, topics)} of all entities the platforms would create."""
    return frozenset(
        (platform.value, description.key, description.device, description.topics)
        for platform, module in platform_modules().items()
        for description in module.descriptions(states)
    )


def _is_structure_topic(topic: str) -> bool:
    return (
        topic in STRUCTURE_TOPICS or topic.endswith("/config") or bool(_STRUCTURE_RE.match(topic))
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Lovelace card."""
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: WarpConfigEntry) -> bool:
    """Set up a WARP charger from a config entry."""
    username = entry.data.get(CONF_USERNAME)
    session = async_create_clientsession(
        hass,
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, True),
        middlewares=(DigestAuthMiddleware(username, entry.data.get(CONF_PASSWORD, "")),)
        if username
        else (),
    )
    client = WarpClient(session, entry.data[CONF_URL])

    async def _run() -> None:
        try:
            await client.run()
        except WarpAuthError:
            entry.async_start_reauth(hass)
        except Exception:
            _LOGGER.exception("WARP client for %s stopped unexpectedly", entry.title)
            raise

    entry.async_create_background_task(hass, _run(), name=f"{DOMAIN} {entry.title} client")
    entry.async_on_unload(client.stop)

    try:
        await client.wait_ready(READY_TIMEOUT)
    except WarpAuthError as err:
        await client.stop()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except WarpConnectionError as err:
        await client.stop()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    runtime = WarpRuntime(hass, entry, client)
    runtime.async_start()
    entry.runtime_data = runtime

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **runtime.device_info()
    )
    runtime.main_device_id = device.id

    initial = structure(client.states)
    _remove_stale_devices(hass, entry, runtime, initial)
    _watch_structure(hass, entry, client, initial)
    _watch_kicks(hass, entry, client)

    await hass.config_entries.async_forward_entry_setups(entry, list(platform_modules()))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WarpConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, list(platform_modules()))
    if unload_ok:
        await entry.runtime_data.client.stop()
    return unload_ok


@callback
def _remove_stale_devices(
    hass: HomeAssistant, entry: WarpConfigEntry, runtime: WarpRuntime, current: Structure
) -> None:
    """Drop devices of this entry that no entity belongs to anymore."""
    valid = {(DOMAIN, runtime.uid)} | {
        (DOMAIN, f"{runtime.uid}_{device}") for _, _, device, _ in current if device
    }
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & valid:
            registry.async_remove_device(device.id)


@callback
def _watch_structure(
    hass: HomeAssistant, entry: WarpConfigEntry, client: WarpClient, initial: Structure
) -> None:
    """Reload (debounced) when the set of entities the device supports changes."""
    cancel_timer: CALLBACK_TYPE | None = None

    @callback
    def _check(_now: datetime) -> None:
        nonlocal cancel_timer
        cancel_timer = None
        if structure(client.states) != initial:
            _LOGGER.info("Device structure of %s changed, reloading", entry.title)
            hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def _schedule() -> None:
        nonlocal cancel_timer
        if cancel_timer is not None:
            cancel_timer()
        cancel_timer = async_call_later(hass, RELOAD_DEBOUNCE, _check)

    @callback
    def _on_message(topic: str, _payload: Any) -> None:
        if _is_structure_topic(topic):
            _schedule()

    @callback
    def _on_connection(connected: bool) -> None:
        if connected:
            _schedule()

    @callback
    def _cleanup() -> None:
        if cancel_timer is not None:
            cancel_timer()

    entry.async_on_unload(client.subscribe_all(_on_message))
    entry.async_on_unload(client.subscribe_connection(_on_connection))
    entry.async_on_unload(_cleanup)


@callback
def _watch_kicks(hass: HomeAssistant, entry: WarpConfigEntry, client: WarpClient) -> None:
    """Raise a repair issue while the device keeps kicking us (5 WebSocket client limit)."""
    issue_id = f"too_many_clients_{entry.entry_id}"

    @callback
    def _update(_connected: bool | None = None) -> None:
        if client.kicked_repeatedly:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="too_many_clients",
                translation_placeholders={"name": entry.title},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    @callback
    def _remove() -> None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    _update()
    entry.async_on_unload(client.subscribe_connection(_update))
    entry.async_on_unload(_remove)
