"""Event platform tests (NFC tag scans)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.tag import DOMAIN as TAG_DOMAIN
from homeassistant.components.tag import EVENT_TAG_SCANNED, TAG_DATA
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from tests.common import (
    FIXTURES,
    Clock,
    check_availability_cycle,
    device_of,
    entity_id,
    frozen_time_fixture,
    get_state,
    platform_fixture,
    push,
    runtime_of,
    seen_tags,
    setup_entry,
    wait_for,
)
from tests.conftest import FakeWarpFactory
from tests.fake_warp import FakeWarp

warp_platform = platform_fixture(Platform.EVENT)
frozen_time = frozen_time_fixture()  # event states are timestamps

TAG_A = "04:A1:B2:C3:D4:E5:F6"
TAG_B = "01:02:03:04"


def _state(hass: HomeAssistant, fake: FakeWarp):
    return get_state(hass, "event", fake, "nfc_tag")


@pytest.mark.parametrize("fixture", [f for f in FIXTURES if f != "wem2"])
async def test_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    fake_warp: FakeWarpFactory,
    fixture: str,
) -> None:
    """The NFC event entity of each charger model."""
    fake = await fake_warp(fixture)
    entry = await setup_entry(hass, fake)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_no_event_entity_without_nfc(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Devices without the nfc feature get no event entity."""
    fake = await fake_warp("wem2")
    entry = await setup_entry(hass, fake)
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []


async def test_scan_fires_event(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, frozen_time: Clock
) -> None:
    """A newly seen tag triggers 'scanned' with tag id and type."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert _state(hass, fake).state == STATE_UNKNOWN

    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 400)))
    state = _state(hass, fake)
    assert datetime.fromisoformat(state.state) == frozen_time.now
    assert state.attributes["event_type"] == "scanned"
    assert state.attributes["event_types"] == ["scanned"]
    assert state.attributes["tag_id"] == TAG_A
    assert state.attributes["tag_type"] == 2


async def test_first_dump_with_recent_tag_is_ignored(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A tag already visible in the very first dump is not a new scan."""
    fake = await fake_warp("warp3", patches={"nfc/seen_tags": seen_tags((2, TAG_A, 300))})
    tags = async_capture_events(hass, EVENT_TAG_SCANNED)
    await setup_entry(hass, fake)
    assert _state(hass, fake).state == STATE_UNKNOWN
    assert tags == []
    # Still being seen (age grows but stays recent) -> still no event.
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 1300)))
    assert _state(hass, fake).state == STATE_UNKNOWN
    assert tags == []


async def test_rescan_after_two_seconds(
    hass: HomeAssistant, fake_warp: FakeWarpFactory, frozen_time: Clock
) -> None:
    """The same tag fires again once its age rose to >= 2000 ms and dropped below again."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    eid = entity_id(hass, "event", fake, "nfc_tag")
    assert eid is not None
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    scans = async_capture_events(hass, EVENT_TAG_SCANNED)

    def fired() -> list[str]:
        return [
            e.data["new_state"].attributes["tag_id"]
            for e in changes
            if e.data["entity_id"] == eid and "tag_id" in e.data["new_state"].attributes
        ]

    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 100)))
    assert fired() == [TAG_A]

    # The tag stays on the reader: age keeps growing below 2 s, then the tag is removed.
    for age in (1100, 1900):
        frozen_time.tick(timedelta(seconds=1))
        await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, age)))
    assert fired() == [TAG_A]
    frozen_time.tick(timedelta(seconds=1))
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 2900)))
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 7000)))
    assert fired() == [TAG_A]

    # Scanned again.
    frozen_time.tick(timedelta(seconds=5))
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 200)))
    assert fired() == [TAG_A, TAG_A]
    await hass.async_block_till_done()
    assert len(scans) == 2


async def test_two_tags_oldest_first(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Two tags that appear in the same message fire in scan order."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    scans = async_capture_events(hass, EVENT_TAG_SCANNED)
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_B, 100), (6, TAG_A, 900), (0, "", 0)))
    await hass.async_block_till_done()
    assert [e.data["tag_id"] for e in scans] == [TAG_A, TAG_B]
    state = _state(hass, fake)
    assert state.attributes["tag_id"] == TAG_B
    assert state.attributes["tag_type"] == 2


async def test_tag_reported_to_home_assistant(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """Scans reach HA's tag integration: bus event with the device, and a stored tag."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    assert TAG_DOMAIN in hass.config.components
    scans = async_capture_events(hass, EVENT_TAG_SCANNED)

    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 100)))
    await hass.async_block_till_done()
    device = device_of(hass, "event", fake, "nfc_tag")
    assert len(scans) == 1
    assert scans[0].data["tag_id"] == TAG_A
    assert scans[0].data["device_id"] == device.id
    stored = hass.data[TAG_DATA].data
    assert TAG_A in stored
    assert stored[TAG_A]["device_id"] == device.id
    assert stored[TAG_A]["last_scanned"] is not None


async def test_tag_service_failure_is_not_fatal(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """If HA's tag integration refuses, the event entity still fires."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    with patch(
        "custom_components.tinkerforge_warp.event.async_scan_tag",
        AsyncMock(side_effect=HomeAssistantError("tag component has not been set up.")),
    ) as scan:
        await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 100)))
        await hass.async_block_till_done()
    scan.assert_awaited_once()
    assert _state(hass, fake).attributes["tag_id"] == TAG_A


async def test_malformed_payload(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Firmware drift in seen_tags does not raise."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    await push(hass, fake, "nfc/seen_tags", [{"tag_id": TAG_A}])
    await push(hass, fake, "nfc/seen_tags", None)
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 3000)))
    assert _state(hass, fake).state == STATE_UNKNOWN


async def test_scan_during_outage_fires_after_reconnect(
    hass: HomeAssistant, fake_warp: FakeWarpFactory
) -> None:
    """A tag scanned < 2 s before the reconnect dump is a real scan and fires once."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    client = runtime_of(hass).client
    scans = async_capture_events(hass, EVENT_TAG_SCANNED)
    await push(hass, fake, "nfc/seen_tags", seen_tags((2, TAG_A, 5000)))

    fake.states["nfc/seen_tags"] = seen_tags((2, TAG_A, 400))  # scanned while we were offline
    await fake.drop_clients(1006)
    await wait_for(lambda: fake.ws_connect_count == 2 and client.connected)
    await hass.async_block_till_done()
    assert len(scans) == 1


async def test_availability(hass: HomeAssistant, fake_warp: FakeWarpFactory) -> None:
    """Unavailable after the grace period, available again after reconnect."""
    fake = await fake_warp("warp3")
    await setup_entry(hass, fake)
    eid = entity_id(hass, "event", fake, "nfc_tag") or ""
    await check_availability_cycle(hass, fake, [eid])
    assert hass.states.get(eid).state != STATE_UNAVAILABLE
