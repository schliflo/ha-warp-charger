"""Update platform: firmware update."""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .entity import States, WarpConfigEntry, WarpEntity, WarpEntityDescription, has_feature

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

CHECK_INTERVAL = timedelta(hours=24)
INSTALL_TIMEOUT = 10  # seconds to wait for the device to react to an install request

# firmware_update/install_state.state (firmware_update/Install State.uint8.enum)
INSTALL_IDLE = 0
INSTALL_IN_PROGRESS = 1
INSTALL_VEHICLE_CONNECTED = 17
INSTALL_REBOOTING = 24
INSTALL_ORIGIN_INSTALL_FIRMWARE = 2

INSTALL_STATE_TOPIC = "firmware_update/install_state"

CHANGELOG = "Tinkerforge/esp32-firmware/master/software/changelog_{}.txt"
CHANGELOG_RAW_URL = f"https://raw.githubusercontent.com/{CHANGELOG}"
CHANGELOG_URL = "https://github.com/" + CHANGELOG.replace("/master/", "/blob/master/", 1)
# info/name type -> changelog file suffix, where they differ
CHANGELOG_NAMES = {"wem": "energy_manager", "wem2": "energy_manager_v2"}
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SECTION = re.compile(r"\n+(?=\d{4}-\d{2}-\d{2}: )")


def _version(text: str | None) -> tuple[int, ...] | None:
    match = _VERSION.search(text or "")
    return tuple(map(int, match.groups())) if match else None


def changelog_since(changelog: str, installed: str | None, latest: str | None) -> str | None:
    """Changelog sections after installed up to latest, newest first, as Markdown."""
    low, high = _version(installed), _version(latest)
    if low is None or high is None:
        return None
    picked = []
    for section in _SECTION.split(changelog.strip()):
        header, _, body = section.partition("\n")
        version = _version(header.partition(": ")[2])
        if version and low < version <= high:
            picked.append(f"**{header}**\n\n{body.strip()}")
    return "\n\n".join(reversed(picked)) or None


@dataclass(frozen=True, kw_only=True)
class WarpUpdateEntityDescription(WarpEntityDescription, UpdateEntityDescription):
    """Update entity description."""


def _value(states: States) -> tuple[str, str | None, int, int]:
    install = states.get(INSTALL_STATE_TOPIC) or {}
    return (
        states["info/version"]["firmware"],
        (states.get("firmware_update/state") or {}).get("update_version"),
        install.get("state", INSTALL_IDLE),
        install.get("progress", 0),
    )


FIRMWARE = WarpUpdateEntityDescription(
    key="firmware",
    translation_key="firmware",
    topics=("info/version", "firmware_update/state", INSTALL_STATE_TOPIC),
    value_fn=_value,
    device_class=UpdateDeviceClass.FIRMWARE,
)


def descriptions(states: States) -> list[WarpUpdateEntityDescription]:
    """Update descriptions the device supports."""
    return [FIRMWARE] if has_feature(states, "firmware_update") else []


class WarpFirmwareUpdate(WarpEntity, UpdateEntity):
    """Firmware update; the device only reports versions newer than the installed one."""

    entity_description: WarpUpdateEntityDescription
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, runtime: Any, description: WarpUpdateEntityDescription) -> None:
        """Initialize the entity."""
        super().__init__(runtime, description)
        self._in_progress = False
        self._percentage: int | None = None
        device_type = (runtime.client.states.get("info/name") or {}).get("type")
        self._changelog = CHANGELOG_NAMES.get(device_type, device_type)
        if self._changelog:
            self._attr_release_url = CHANGELOG_URL.format(self._changelog)

    @property
    def in_progress(self) -> bool:
        """Return True while the device installs (kept apart from the base-class reset)."""
        return self._in_progress

    @property
    def update_percentage(self) -> int | None:
        """Return the install progress."""
        return self._percentage

    @callback
    def _update_from_value(self, value: Any) -> None:
        installed, update_version, state, progress = value or (None, None, INSTALL_IDLE, 0)
        self._attr_installed_version = installed
        self._attr_latest_version = update_version or installed
        self._in_progress = state == INSTALL_IN_PROGRESS
        self._percentage = progress if self._in_progress else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """The device only offers newer versions, and its versions are not PEP 440."""
        return latest_version != installed_version

    async def async_release_notes(self) -> str | None:
        """Changelog entries between the installed and the latest version, from GitHub."""
        if not self._changelog:
            return None
        try:
            async with async_get_clientsession(self.hass).get(
                CHANGELOG_RAW_URL.format(self._changelog), raise_for_status=True
            ) as resp:
                changelog = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Fetching the firmware changelog failed: %s", err)
            return None
        return changelog_since(changelog, self.installed_version, self.latest_version)

    async def async_added_to_hass(self) -> None:
        """Subscribe, then check for updates now and every 24 h."""
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._check, CHECK_INTERVAL))
        self.runtime.entry.async_create_background_task(
            self.hass, self._check(), name=f"{DOMAIN} firmware check"
        )

    async def _check(self, _now: datetime | None = None) -> None:
        """Ask the device to look for a new firmware; failures are only logged."""
        if self._in_progress:
            return
        try:
            await self.runtime.async_put("firmware_update/check_for_update", None)
        except HomeAssistantError as err:
            _LOGGER.debug("Firmware update check failed: %s", err)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install a firmware; the device answers 200 and reports refusals via install_state."""
        result: list[int] = []
        changed = asyncio.Event()

        @callback
        def _on_state(payload: Any) -> None:
            state = payload.get("state", INSTALL_IDLE)
            if state != INSTALL_IDLE and not result:
                result.append(state)
                changed.set()

        unsubscribe = self.runtime.client.subscribe(INSTALL_STATE_TOPIC, _on_state)
        try:
            await self.runtime.async_put(
                "firmware_update/install_firmware", {"version": version or self.latest_version}
            )
            try:
                async with asyncio.timeout(INSTALL_TIMEOUT):
                    await changed.wait()
            except TimeoutError:
                # The device does not push an unchanged state: a repeated refusal stays silent.
                current = self.runtime.client.states.get(INSTALL_STATE_TOPIC) or {}
                if current.get("origin") == INSTALL_ORIGIN_INSTALL_FIRMWARE:
                    result.append(current.get("state", INSTALL_IDLE))
        finally:
            unsubscribe()

        state = result[0] if result else INSTALL_IDLE
        if state in (INSTALL_IDLE, INSTALL_IN_PROGRESS, INSTALL_REBOOTING):
            return
        if state == INSTALL_VEHICLE_CONNECTED:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="install_vehicle_connected"
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="install_failed",
            translation_placeholders={"state": str(state)},
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: WarpConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the update entity."""
    runtime = entry.runtime_data
    async_add_entities(WarpFirmwareUpdate(runtime, d) for d in descriptions(runtime.client.states))
