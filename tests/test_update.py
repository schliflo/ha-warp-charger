"""Update platform tests (firmware)."""

import logging
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_RELEASE_URL,
    ATTR_UPDATE_PERCENTAGE,
    SERVICE_INSTALL,
)
from homeassistant.components.update import (
    DOMAIN as UPDATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events, snapshot_platform
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator
from syrupy.assertion import SnapshotAssertion

from custom_components.tinkerforge_warp.const import DOMAIN
from custom_components.tinkerforge_warp.update import (
    CHANGELOG_RAW_URL,
    CHANGELOG_URL,
    CHECK_INTERVAL,
)
from tests.common import (
    FIXTURES,
    check_availability_cycle,
    entity_id,
    fire,
    get_state,
    merge,
    platform_fixture,
    push,
    reject,
    setup_entry,
    wait_for,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import FakeWarp, InstallState

warp_platform = platform_fixture(Platform.UPDATE)

CHECK = ("firmware_update/check_for_update", None)
INSTALL = "firmware_update/install_firmware"
LATEST = "2.13.5+6abe4b1e"


async def _setup(hass: HomeAssistant, fake: FakeWarp) -> None:
    """Set up and wait for the initial update check and its echo."""
    await setup_entry(hass, fake)
    await wait_for(lambda: CHECK in fake.puts)
    await hass.async_block_till_done()


async def _install(hass: HomeAssistant, fake: FakeWarp, version: str | None = None) -> None:
    eid = entity_id(hass, "update", fake, "firmware")
    assert eid is not None
    data: dict[str, str] = {ATTR_ENTITY_ID: eid}
    if version:
        data["version"] = version
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, data, blocking=True)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, fake: FakeWarp):
    return get_state(hass, "update", fake, "firmware")


def _installs(fake: FakeWarp) -> list[tuple[str, object]]:
    return [p for p in fake.puts if p[0] == INSTALL]


@pytest.mark.parametrize("fixture", FIXTURES)
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """The firmware entity of each device model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_versions_and_check_at_setup(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The real WARP3 runs 2.12.1 and the device knows 2.13.5; a check is requested at setup."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)
    assert fake.puts == [CHECK]
    state = _state(hass, fake)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_INSTALLED_VERSION] == "2.12.1+6a4f9137"
    assert state.attributes[ATTR_LATEST_VERSION] == LATEST
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_UPDATE_PERCENTAGE] is None


async def test_up_to_date(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """No update_version -> latest is the installed version."""
    fake = await fake_warp("warp2")
    await _setup(hass, fake)
    state = _state(hass, fake)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_LATEST_VERSION] == state.attributes[ATTR_INSTALLED_VERSION]


async def test_new_version_announced(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A later check result and a finished installation show up."""
    fake = await fake_warp("warp2")
    await _setup(hass, fake)
    await merge(hass, fake, "firmware_update/state", update_version="2.14.0+abcdef01")
    assert _state(hass, fake).state == STATE_ON
    assert _state(hass, fake).attributes[ATTR_LATEST_VERSION] == "2.14.0+abcdef01"
    await push(
        hass, fake, "info/version", {**fake.states["info/version"], "firmware": "2.14.0+abcdef01"}
    )
    await merge(hass, fake, "firmware_update/state", update_version="")
    assert _state(hass, fake).state == STATE_OFF
    assert _state(hass, fake).attributes[ATTR_INSTALLED_VERSION] == "2.14.0+abcdef01"


async def test_periodic_check(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The device never checks on its own: HA asks every 24 h."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)
    assert timedelta(hours=24) == CHECK_INTERVAL
    await fire(hass, CHECK_INTERVAL.total_seconds() + 1)
    await wait_for(lambda: fake.puts.count(CHECK) == 2)


async def test_check_failure_is_not_fatal(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected check is only logged at debug level."""
    fake = await fake_warp("warp3")
    reject(fake, "firmware_update/check_for_update", "No network")
    caplog.set_level(logging.DEBUG, logger="custom_components.tinkerforge_warp.update")
    await _setup(hass, fake)
    assert _state(hass, fake).state == STATE_ON
    assert "Firmware update check failed" in caplog.text
    assert [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components")
    ] == []


async def test_install_with_vehicle_connected(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """The device answers 200 but reports 'vehicle connected' via install_state."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)
    with pytest.raises(HomeAssistantError) as exc:
        await _install(hass, fake)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "install_vehicle_connected"
    assert _installs(fake) == [(INSTALL, {"version": LATEST})]
    assert _state(hass, fake).attributes[ATTR_IN_PROGRESS] is False


async def test_install_progress(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Without a car: installation starts, progress is reported, then the device reboots."""
    fake = await fake_warp("warp3", install_step_delay=0.05)
    await _setup(hass, fake)
    await fake.unplug()
    await hass.async_block_till_done()
    eid = entity_id(hass, "update", fake, "firmware") or ""
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    await _install(hass, fake)
    assert _installs(fake) == [(INSTALL, {"version": LATEST})]

    def progress() -> list[tuple[bool, int | None]]:
        return [
            (
                e.data["new_state"].attributes[ATTR_IN_PROGRESS],
                e.data["new_state"].attributes[ATTR_UPDATE_PERCENTAGE],
            )
            for e in changes
            if e.data["entity_id"] == eid
        ]

    await wait_for(lambda: progress()[-1] == (False, None))
    seen = progress()
    # in_progress while installing, percentages ascend, and it ends with the reboot state.
    assert seen[-1] == (False, None)
    percentages = [p for active, p in seen if active and p is not None]
    assert percentages == sorted(percentages)
    assert {0, 50, 100} <= set(percentages)
    assert fake.states["firmware_update/install_state"]["state"] == InstallState.REBOOTING


async def test_install_survives_reboot(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The reboot after a successful installation does not make the entity unavailable."""
    fake = await fake_warp("warp3", install_reboots=True)
    await _setup(hass, fake)
    await fake.unplug()
    await _install(hass, fake)
    await wait_for(lambda: fake.reboot_count == 1)
    await wait_for(lambda: fake.ws_connect_count == 2)
    await hass.async_block_till_done()
    await fire(hass, 31)
    assert _state(hass, fake).state != STATE_UNAVAILABLE


@pytest.mark.parametrize(
    "error", [InstallState.DOWNLOAD_ERROR, InstallState.SIGNATURE_VERIFY_FAILED, InstallState.BUSY]
)
async def test_install_failed(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, error: InstallState
) -> None:
    """Any other refusal becomes install_failed with the state number."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)

    async def refuse(_body: object) -> None:
        await fake._set_install(error, 0)

    fake._commands[INSTALL] = refuse
    with pytest.raises(HomeAssistantError) as exc:
        await _install(hass, fake)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "install_failed"
    assert exc.value.translation_placeholders == {"state": str(int(error))}


async def test_install_http_error(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """A 400 for the install request itself."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)
    reject(fake, INSTALL, "Version is malformed")
    with pytest.raises(HomeAssistantError) as exc:
        await _install(hass, fake)
    assert exc.value.translation_key == "command_failed"
    assert exc.value.translation_placeholders == {"error": "Version is malformed"}


async def test_install_silent_repeated_refusal(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """An unchanged install_state is not pushed again: the stored refusal is used after timeout."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)

    async def silent(_body: object) -> None:
        return None

    fake._commands[INSTALL] = silent
    await push(
        hass,
        fake,
        "firmware_update/install_state",
        {"origin": 2, "state": InstallState.VEHICLE_CONNECTED, "progress": 0},
    )
    with (
        patch("custom_components.tinkerforge_warp.update.INSTALL_TIMEOUT", 0.1),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await _install(hass, fake)
    assert exc.value.translation_key == "install_vehicle_connected"


async def test_install_no_reaction_is_not_an_error(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """If the device never reacts (and shows no refusal) the call just returns."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)

    async def silent(_body: object) -> None:
        return None

    fake._commands[INSTALL] = silent
    with patch("custom_components.tinkerforge_warp.update.INSTALL_TIMEOUT", 0.1):
        await _install(hass, fake)
    assert _state(hass, fake).attributes[ATTR_IN_PROGRESS] is False


async def test_no_check_while_installing(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """The 24 h check is skipped during an installation (it would reset the install state)."""
    fake = await fake_warp("warp3", install_step_delay=10)
    await _setup(hass, fake)
    await fake.unplug()
    await _install(hass, fake)
    assert _state(hass, fake).attributes[ATTR_IN_PROGRESS] is True
    await fire(hass, CHECK_INTERVAL.total_seconds() + 1)
    assert fake.puts.count(CHECK) == 1


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3")
    await _setup(hass, fake)
    eid = entity_id(hass, "update", fake, "firmware") or ""
    await check_availability_cycle(hass, fake, [eid])
    assert _state(hass, fake).state == STATE_ON


@pytest.fixture
def changelog(hass: HomeAssistant) -> Iterator[AiohttpClientMocker]:
    """Mock only the changelog download; the device keeps its real connection."""
    mocker = AiohttpClientMocker()
    with patch(
        "custom_components.tinkerforge_warp.update.async_get_clientsession",
        return_value=mocker.create_session(hass.loop),
    ):
        yield mocker


CHANGELOG_TEXT = """2026-07-09: 2.12.1 (e04a6ee0)
- Old fix

2026-09-10: 2.13.0 (6870f31b)
- New feature

2026-10-01: 2.13.5 (098f202e)
- Latest fix

2026-10-27: 2.13.6 (afa5c782)
- Not offered yet
"""


async def test_release_notes(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    changelog: AiohttpClientMocker,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Release notes list the changelog entries after the installed up to the offered version."""
    changelog.get(CHANGELOG_RAW_URL.format("warp3"), text=CHANGELOG_TEXT)
    fake = await fake_warp("warp3")
    await _setup(hass, fake)
    state = _state(hass, fake)
    assert state.attributes[ATTR_RELEASE_URL] == CHANGELOG_URL.format("warp3")

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "update/release_notes", "entity_id": state.entity_id})
    result = await client.receive_json()
    assert result["result"] == (
        "**2026-10-01: 2.13.5 (098f202e)**\n\n- Latest fix\n\n"
        "**2026-09-10: 2.13.0 (6870f31b)**\n\n- New feature"
    )


async def test_release_notes_unavailable(
    hass: HomeAssistant,
    fake_warp: FakeWarpFactory,
    changelog: AiohttpClientMocker,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A failed changelog download gives no release notes instead of an error."""
    changelog.get(CHANGELOG_RAW_URL.format("energy_manager_v2"), status=404)
    fake = await fake_warp("wem2")
    await _setup(hass, fake)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "update/release_notes", "entity_id": _state(hass, fake).entity_id}
    )
    result = await client.receive_json()
    assert result["success"] is True
    assert result["result"] is None
