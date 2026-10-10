"""Shared helpers for the platform tests."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_VERIFY_SSL, STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.tinkerforge_warp.const import CONF_URL, DOMAIN, GRACE_PERIOD
from custom_components.tinkerforge_warp.entity import WarpRuntime
from tests.conftest import FakeWarpFactory
from tests.fake_warp import FakeWarp, HttpError
from warp_charger import WarpClient

FIXTURES = ["warp3", "warp2", "warp4", "wem2"]
FROZEN_TIME = "2026-10-10 12:00:00+00:00"

FAST_CLIENT = partial(
    WarpClient,
    reconnect_delay=0.05,
    max_reconnect_delay=0.05,
    going_away_delay=0.05,
    jitter=0,
    receive_timeout=1000,
)


def platform_fixture(platform: Platform) -> Callable[..., Any]:
    """Autouse fixture: fast reconnects, only ``platform`` forwarded, all entities enabled.

    The patches stay active for the whole test: unload and the structure watch call
    ``platform_modules()`` again. Loaded entries are unloaded at teardown.
    """

    @pytest.fixture(autouse=True, name="warp_platform")
    async def _fixture(
        hass: HomeAssistant,
        fake_warp: FakeWarpFactory,
    ) -> AsyncGenerator[None]:
        with (
            patch(
                "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
                PropertyMock(return_value=True),
            ),
            patch("custom_components.tinkerforge_warp.WarpClient", FAST_CLIENT),
            patch("custom_components.tinkerforge_warp.PLATFORMS", [platform]),
        ):
            yield
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.state is ConfigEntryState.LOADED:
                    await hass.config_entries.async_unload(entry.entry_id)

    return _fixture


class Clock:
    """Controllable replacement for ``dt_util.utcnow`` (freezegun would freeze the event loop)."""

    def __init__(self) -> None:
        """Start at ``FROZEN_TIME``."""
        self.now = datetime.fromisoformat(FROZEN_TIME)

    def tick(self, delta: timedelta) -> None:
        """Advance the clock."""
        self.now += delta

    def move_to(self, now: datetime) -> None:
        """Set the clock."""
        self.now = now


def frozen_time_fixture() -> Callable[..., Any]:
    """Autouse fixture: ``dt_util.utcnow()`` returns a fixed time; the event loop keeps running."""

    @pytest.fixture(autouse=True, name="frozen_time")
    def _fixture() -> Generator[Clock]:
        clock = Clock()
        with patch("homeassistant.util.dt.utcnow", lambda: clock.now):
            yield clock

    return _fixture


async def setup_entry(hass: HomeAssistant, fake: FakeWarp, **data: Any) -> MockConfigEntry:
    """Create and set up a config entry for ``fake``."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake.states["info/name"]["uid"],
        title=fake.states["info/display_name"]["display_name"],
        data={CONF_URL: fake.base_url, CONF_VERIFY_SSL: False, **data},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def wait_for(predicate: Callable[[], bool], limit: float = 5) -> None:
    """Wait (real time) until ``predicate()`` holds."""
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def fire(hass: HomeAssistant, seconds: float) -> None:
    """Fire timers that are due in ``seconds``."""
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def runtime_of(hass: HomeAssistant) -> WarpRuntime:
    """Return the runtime of the single config entry."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry.runtime_data


_sync_counter = 0


async def flush(hass: HomeAssistant, fake: FakeWarp) -> None:
    """Wait until everything pushed so far was processed by the client and the entities."""
    global _sync_counter  # noqa: PLW0603
    _sync_counter += 1
    marker = {"n": _sync_counter}
    await fake.push("test/sync", marker)
    client = runtime_of(hass).client
    await wait_for(lambda: client.states.get("test/sync") == marker)
    await hass.async_block_till_done()


async def push(hass: HomeAssistant, fake: FakeWarp, topic: str, payload: Any) -> None:
    """Push a state from the device and wait until entities processed it."""
    await fake.push(topic, payload)
    await flush(hass, fake)


async def merge(hass: HomeAssistant, fake: FakeWarp, topic: str, **fields: Any) -> None:
    """Push ``topic`` with ``fields`` merged into its current payload."""
    await push(hass, fake, topic, {**fake.states[topic], **fields})


def entity_id(hass: HomeAssistant, domain: str, fake: FakeWarp, key: str) -> str | None:
    """Entity id of the entity with ``key`` (None if it does not exist)."""
    uid = fake.states["info/name"]["uid"]
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{uid}_{key}")


def get_state(hass: HomeAssistant, domain: str, fake: FakeWarp, key: str) -> State:
    """State of the entity with ``key``."""
    eid = entity_id(hass, domain, fake, key)
    assert eid is not None, f"no {domain} entity with key {key}"
    state = hass.states.get(eid)
    assert state is not None
    return state


def device_of(hass: HomeAssistant, domain: str, fake: FakeWarp, key: str) -> dr.DeviceEntry:
    """Device the entity with ``key`` belongs to."""
    eid = entity_id(hass, domain, fake, key)
    assert eid is not None
    entry = er.async_get(hass).async_get(eid)
    assert entry is not None
    assert entry.device_id is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    return device


def seen_tags(*tags: tuple[int, str, int], size: int = 9) -> list[dict[str, Any]]:
    """``nfc/seen_tags`` payload from ``(tag_type, tag_id, last_seen_ms)``, padded with empties."""
    result = [{"tag_type": t, "tag_id": i, "last_seen": age} for t, i, age in tags]
    result += [{"tag_type": 0, "tag_id": "", "last_seen": 0}] * (size - len(result))
    return result


async def check_availability_cycle(
    hass: HomeAssistant, fake: FakeWarp, entity_ids: list[str]
) -> None:
    """A disconnect longer than the grace period makes entities unavailable until reconnect."""
    assert entity_ids
    runtime = runtime_of(hass)
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in entity_ids)

    fake.hold_dump = True  # reconnects never complete the dump
    await fake.drop_clients(1006)
    await wait_for(lambda: not runtime.client.connected)
    await fire(hass, GRACE_PERIOD - 1)
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in entity_ids)
    await fire(hass, GRACE_PERIOD + 1)
    assert all(hass.states.get(e).state == STATE_UNAVAILABLE for e in entity_ids)

    fake.hold_dump = False
    await fake.drop_clients(1006)
    await wait_for(lambda: runtime.available)
    await hass.async_block_till_done()
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in entity_ids)


def reject(fake: FakeWarp, path: str, text: str = "Rejected by test") -> None:
    """Make the fake device answer ``path`` (command or ``*_update``) with HTTP 400."""

    async def handler(_body: Any) -> None:
        raise HttpError(400, text)

    if path in fake._commands:
        fake._commands[path] = handler
    else:
        fake._updates[path.removesuffix("_update")] = handler
