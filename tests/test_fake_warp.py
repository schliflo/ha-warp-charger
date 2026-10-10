"""The fake WARP device itself: dump framing, push, auth, redirect and command quirks."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp
import pytest

from tests.conftest import FakeWarpFactory
from tests.fake_warp import REMOVE, FakeWarp, InstallState, load_fixture, patch

FIXTURE_NAMES = ["warp2", "warp3", "warp4", "wem2"]
CREDS = ("admin", "s3cret")


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as sess:
        yield sess


def digest_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(middlewares=(aiohttp.DigestAuthMiddleware(*CREDS),))


async def read_dump(ws: aiohttp.ClientWebSocketResponse, wait: float = 5) -> list[str]:
    """Collect text frames up to and including the one containing the end marker."""
    frames: list[str] = []
    while True:
        frame = await ws.receive_str(timeout=wait)
        frames.append(frame)
        if "\n\n" in frame:
            return frames


def parse(frames: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for frame in frames for line in frame.split("\n") if line]


async def next_msg(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    return json.loads((await ws.receive_str(timeout=5)).strip())


async def put(
    session: aiohttp.ClientSession, fake: FakeWarp, path: str, payload: Any = None
) -> aiohttp.ClientResponse:
    async with session.put(f"{fake.base_url}/{path}", json=payload) as resp:
        await resp.read()
        return resp


async def put_text(
    session: aiohttp.ClientSession, fake: FakeWarp, path: str, payload: Any = None
) -> tuple[int, str]:
    async with session.put(f"{fake.base_url}/{path}", json=payload) as resp:
        return resp.status, await resp.text()


# ---------------------------------------------------------------- fixtures


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_wellformed(name: str) -> None:
    messages = load_fixture(name)
    topics = [m["topic"] for m in messages]
    assert len(topics) == len(set(topics))
    assert topics[0] == "info/keep_alive"
    assert all(set(m) == {"topic", "payload"} for m in messages)


def test_fixture_model_differences() -> None:
    def by_topic(name: str) -> dict[str, Any]:
        return {m["topic"]: m["payload"] for m in load_fixture(name)}

    w2, w3, w4, wem = (by_topic(n) for n in ("warp2", "warp3", "warp4", "wem2"))
    assert w3["info/name"]["type"] == "warp3"
    assert w2["info/name"]["uid"] == "2jEx"
    assert w2["evse/hardware_configuration"]["evse_version"] == 20
    assert "phase_switch" not in w2["info/features"]
    assert "rgb_led" not in w2["info/features"]
    assert w4["ev/state"]["soc"] == 55.5
    assert "soc_pct" in w4["charge_limits/active_limits"]
    assert w4["meters/1/config"][0] == 9
    assert "evse" not in wem["info/features"]
    assert not [t for t in wem if t.startswith("evse/")]
    assert wem["power_manager/charge_mode"] == {"mode": 2}
    assert wem["day_ahead_prices/state"]["current_price"] == 25123


def test_patch() -> None:
    base = load_fixture("warp3")
    out = patch(
        base,
        {
            "evse/state": {"charger_state": 0},
            "info/features": ["evse"],
            "evse/boost_mode": REMOVE,
            "new/topic": {"a": 1},
        },
    )
    by_topic = {m["topic"]: m["payload"] for m in out}
    assert by_topic["evse/state"]["charger_state"] == 0
    assert by_topic["evse/state"]["iec61851_state"] == 1  # untouched member
    assert by_topic["info/features"] == ["evse"]
    assert "evse/boost_mode" not in by_topic
    assert by_topic["new/topic"] == {"a": 1}
    assert {m["topic"]: m["payload"] for m in base}["info/features"] != ["evse"]  # no mutation


# ---------------------------------------------------------------- websocket


async def test_dump_framing_and_end_marker(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", frame_size=3)
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        frames = await read_dump(ws)
    expected = load_fixture("warp3")
    assert parse(frames) == expected
    assert frames[0].count("\n") == 3  # three messages per frame
    assert frames[-1].endswith("\n\n")
    assert all("\n\n" not in f for f in frames[:-1])
    assert fake.ws_connect_count == 1


async def test_hold_dump_never_sends_end_marker(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", hold_dump=True)
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        with pytest.raises(TimeoutError):
            await read_dump(ws, wait=0.5)  # frames arrive, marker never does
        await fake.push("evse/boost_mode", {"enabled": True})  # queued, not sent mid-dump


async def test_push_after_dump(fake_warp: FakeWarpFactory, session: aiohttp.ClientSession) -> None:
    fake = await fake_warp("warp3")
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        await fake.push("evse/boost_mode", {"enabled": True})
        frame = await ws.receive_str(timeout=5)
    assert frame == '{"topic": "evse/boost_mode", "payload": {"enabled": true}}\n'
    assert fake.states["evse/boost_mode"] == {"enabled": True}


async def test_push_during_slow_dump_is_delivered_after_it(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", frame_size=100, dump_delay=0.2)
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await asyncio.sleep(0.05)
        await fake.push("evse/boost_mode", {"enabled": True})
        await read_dump(ws)
        assert (await next_msg(ws))["topic"] == "evse/boost_mode"


async def test_reconnect_dump_reflects_current_state(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    await fake.push("evse/boost_mode", {"enabled": True})
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        topics = {m["topic"]: m["payload"] for m in parse(await read_dump(ws))}
    assert topics["evse/boost_mode"] == {"enabled": True}


async def test_drop_clients_going_away(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        await fake.drop_clients(1001)
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert ws.close_code == 1001


async def test_drop_clients_abnormal(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        await fake.drop_clients(1006)
        msg = await ws.receive(timeout=5)
        assert msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR)
        assert ws.close_code != 1001


@pytest.mark.parametrize("status", [401, 500])
async def test_reject_ws_status(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession, status: int
) -> None:
    fake = await fake_warp("warp3", reject_ws_status=status)
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await session.ws_connect(f"{fake.base_url}/ws")
    assert err.value.status == status


async def test_max_clients_kicks_oldest(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", max_clients=2)
    ws1 = await session.ws_connect(f"{fake.base_url}/ws")
    await read_dump(ws1)
    ws2 = await session.ws_connect(f"{fake.base_url}/ws")
    await read_dump(ws2)
    ws3 = await session.ws_connect(f"{fake.base_url}/ws")
    await read_dump(ws3)
    msg = await ws1.receive(timeout=5)
    assert msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE)
    assert fake.kick_count == 1
    assert not ws2.closed
    for ws in (ws1, ws2, ws3):
        await ws.close()


async def test_keep_alive_interval(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", keep_alive_interval=0.05)
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        msg = await next_msg(ws)
    assert msg["topic"] == "info/keep_alive"


# ---------------------------------------------------------------- HTTP basics


async def test_http_get(fake_warp: FakeWarpFactory, session: aiohttp.ClientSession) -> None:
    fake = await fake_warp("warp3")
    async with session.get(f"{fake.base_url}/info/name") as resp:
        assert resp.status == 200
        assert (await resp.json())["uid"] == "2jEx"
    async with session.get(f"{fake.base_url}/credential_check") as resp:
        assert (resp.status, await resp.text()) == (200, "Credentials okay")
    async with session.get(f"{fake.base_url}/login_state") as resp:
        assert await resp.text() == "Logged in"
    async with session.get(f"{fake.base_url}/nope/nope") as resp:
        assert (resp.status, await resp.text()) == (404, "Nothing matches the given URI.")


async def test_put_unknown_and_garbage(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    assert (await put_text(session, fake, "nope/nope", 1))[0] == 404
    async with session.put(f"{fake.base_url}/evse/boost_mode_update", data=b"{not json") as resp:
        assert resp.status == 400


# ---------------------------------------------------------------- auth / redirect


async def test_digest_http_and_ws(fake_warp: FakeWarpFactory) -> None:
    fake = await fake_warp("warp3", auth=CREDS)
    async with digest_session() as sess:
        async with sess.get(f"{fake.base_url}/credential_check") as resp:
            assert resp.status == 200
        async with sess.get(f"{fake.base_url}/info/name") as resp:
            assert resp.status == 200
        resp = await put(sess, fake, "evse/boost_mode_update", {"enabled": True})
        assert resp.status == 200
        assert fake.states["evse/boost_mode"] == {"enabled": True}
        async with sess.ws_connect(f"{fake.base_url}/ws") as ws:
            assert parse(await read_dump(ws))[0]["topic"] == "info/keep_alive"


async def test_digest_rejects_missing_and_wrong_credentials(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", auth=CREDS)
    async with session.get(f"{fake.base_url}/info/name") as resp:
        assert resp.status == 401
        challenge = resp.headers["WWW-Authenticate"]
    assert challenge.startswith('Digest realm="esp32-lib", qop="auth", nonce="')
    assert 'opaque="' in challenge
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await session.ws_connect(f"{fake.base_url}/ws")
    assert err.value.status == 401
    async with aiohttp.ClientSession(
        middlewares=(aiohttp.DigestAuthMiddleware("admin", "wrong"),)
    ) as bad:
        async with bad.get(f"{fake.base_url}/credential_check") as resp:
            assert resp.status == 401
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await bad.ws_connect(f"{fake.base_url}/ws")
        assert err.value.status == 401
    assert fake.puts == []


async def test_redirect(fake_warp: FakeWarpFactory, session: aiohttp.ClientSession) -> None:
    target = await fake_warp("warp3")
    fake = await fake_warp("warp3", redirect_to=target.base_url)
    async with session.get(f"{fake.base_url}/info/name", allow_redirects=False) as resp:
        assert resp.status == 307
        assert resp.headers["Location"] == f"{target.base_url}/info/name"
    async with session.get(f"{fake.base_url}/info/name") as resp:
        assert resp.status == 200
        assert str(resp.url) == f"{target.base_url}/info/name"
    await put(session, fake, "evse/boost_mode_update", {"enabled": True})  # 307 keeps method+body
    assert target.puts == [("evse/boost_mode_update", {"enabled": True})]
    assert fake.puts == []


# ---------------------------------------------------------------- commands


async def test_start_stop_without_vehicle_is_silent_noop(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp2")  # no car
    before = json.dumps(fake.states)
    for path in ("evse/start_charging", "evse/stop_charging"):
        resp = await put(session, fake, path)
        assert resp.status == 200
    assert json.dumps(fake.states) == before
    assert [p for p, _ in fake.puts] == ["evse/start_charging", "evse/stop_charging"]


async def test_start_stop_with_vehicle(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp2")
    await fake.plug_in()  # auto start + 32 A: charging immediately
    assert fake.states["evse/state"]["charger_state"] == 3
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        await put(session, fake, "evse/stop_charging")
        assert fake.states["evse/state"]["charger_state"] == 1
        assert fake.states["evse/state"]["allowed_charging_current"] == 0
        topics = {(await next_msg(ws))["topic"] for _ in range(2)}
        assert topics == {"evse/slots", "evse/state"}
    await put(session, fake, "evse/start_charging")
    assert fake.states["evse/state"]["charger_state"] == 3
    assert fake.states["evse/state"]["allowed_charging_current"] == 32000
    await fake.unplug()
    assert fake.states["evse/state"]["charger_state"] == 0


async def test_external_current_and_shorthand(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp2")
    await fake.plug_in()
    await put(session, fake, "evse/external_current_update", {"current": 8000})
    assert fake.states["evse/external_current"] == {"current": 8000}
    assert fake.states["evse/state"]["allowed_charging_current"] == 8000
    await put(session, fake, "evse/external_current_update", 0)  # bare value shorthand
    assert fake.states["evse/external_current"] == {"current": 0}
    assert fake.states["evse/state"]["charger_state"] == 1
    # invalid-range currents are still applied, like the firmware
    resp = await put(session, fake, "evse/external_current_update", {"current": 3000})
    assert resp.status == 200
    assert fake.states["evse/external_current"] == {"current": 3000}


async def test_external_enabled_forces_32a(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3", patches={"evse/external_current": {"current": 8000}})
    await put(session, fake, "evse/external_enabled_update", {"enabled": False})
    assert fake.states["evse/external_enabled"] == {"enabled": True}
    assert fake.states["evse/external_current"] == {"current": 32000}


async def test_config_switches_and_type_checks(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    for path, payload, topic in [
        ("evse/boost_mode_update", {"enabled": True}, "evse/boost_mode"),
        ("evse/phase_auto_switch_update", {"enabled": True}, "evse/phase_auto_switch"),
        (
            "evse/external_clear_on_disconnect_update",
            {"clear_on_disconnect": True},
            "evse/external_clear_on_disconnect",
        ),
        (
            "evse/auto_start_charging_update",
            {"auto_start_charging": False},
            "evse/auto_start_charging",
        ),
    ]:
        assert (await put(session, fake, path, payload)).status == 200
        assert fake.states[topic] == payload
    assert (await put_text(session, fake, "evse/boost_mode_update", {"enabled": 1}))[0] == 400
    assert (await put_text(session, fake, "evse/boost_mode_update", {"bogus": True}))[0] == 400


async def test_charge_mode_validation(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")  # supported: [1, 0]
    assert (await put_text(session, fake, "power_manager/charge_mode_update", {"mode": 2}))[
        0
    ] == 400
    assert (await put_text(session, fake, "power_manager/charge_mode_update", {"mode": 1}))[
        0
    ] == 200
    assert fake.states["power_manager/charge_mode"] == {"mode": 1}
    # evse/charge_mode needs charge management on the EVSE
    assert (await put_text(session, fake, "evse/charge_mode_update", {"mode": 0}))[0] == 400
    fake2 = await fake_warp(
        "warp3",
        patches={
            "evse/management_enabled": {"enabled": True},
            "evse/supported_charge_modes": [0, 1, 2],
        },
    )
    assert (await put_text(session, fake2, "evse/charge_mode_update", {"mode": 2}))[0] == 200
    assert fake2.states["evse/charge_mode"] == {"mode": 2}
    assert (await put_text(session, fake2, "evse/charge_mode_update", {"mode": 3}))[0] == 400


async def test_external_control_phases(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    status, text = await put_text(
        session, fake, "power_manager/external_control_update", {"phases_wanted": 2}
    )
    assert (status, text) == (400, "Invalid value for phases_wanted! Only 1 or 3 are allowed.")
    assert (
        await put_text(session, fake, "power_manager/external_control_update", {"phases_wanted": 3})
    )[0] == 200
    assert fake.states["power_manager/external_control"] == {"phases_wanted": 3}
    assert fake.states["evse/low_level_state"]["phases_requested"] == 3
    assert fake.states["evse/low_level_state"]["phases_current"] == 3
    # WARP2 has no phase_switch feature -> command does not exist
    fake2 = await fake_warp("warp2")
    assert (
        await put_text(
            session, fake2, "power_manager/external_control_update", {"phases_wanted": 3}
        )
    )[0] == 404


async def test_charge_limits(fake_warp: FakeWarpFactory, session: aiohttp.ClientSession) -> None:
    fake = await fake_warp("warp3")
    assert (await put_text(session, fake, "charge_limits/override_duration", {"duration": 11}))[
        0
    ] == 400
    await put(session, fake, "charge_limits/override_duration", {"duration": 4})
    await put(session, fake, "charge_limits/override_energy", {"energy_wh": 5000})
    assert fake.states["charge_limits/active_limits"] == {"duration": 4, "energy_wh": 5000}
    state = fake.states["charge_limits/state"]
    assert state["target_timestamp_ms"] == state["start_timestamp_ms"] + 3_600_000
    assert state["target_energy_kwh"] == pytest.approx(state["start_energy_kwh"] + 5)
    assert (await put_text(session, fake, "charge_limits/override_soc", {"soc_pct": 80}))[0] == 404
    assert (await put_text(session, fake, "charge_limits/restart"))[0] == 200

    fake4 = await fake_warp("warp4")
    await put(session, fake4, "charge_limits/override_soc", {"soc_pct": 80})
    assert fake4.states["charge_limits/active_limits"]["soc_pct"] == 80
    assert fake4.states["charge_limits/state"]["target_soc_pct"] == 80


async def test_inject_soc(fake_warp: FakeWarpFactory, session: aiohttp.ClientSession) -> None:
    fake = await fake_warp("warp4")
    assert (await put_text(session, fake, "ev/inject_soc", {"soc": 101}))[0] == 400
    await put(session, fake, "ev/inject_soc", {"soc": 71.5})
    assert fake.states["ev/state"]["soc"] == 71.5
    assert fake.states["meters/1/values"][0] == 71.5
    fake3 = await fake_warp("warp3")
    assert (await put_text(session, fake3, "ev/inject_soc", {"soc": 50}))[0] == 404


async def test_inject_tag(fake_warp: FakeWarpFactory, session: aiohttp.ClientSession) -> None:
    fake = await fake_warp("warp3")
    assert (await put_text(session, fake, "nfc/inject_tag", {"tag_type": 2, "tag_id": "zz"}))[
        0
    ] == 400
    await put(session, fake, "nfc/inject_tag", {"tag_type": 2, "tag_id": "01:23:ab:3d"})
    assert fake.states["nfc/seen_tags"][8] == {
        "tag_type": 2,
        "tag_id": "01:23:AB:3D",
        "last_seen": 0,
    }
    wem = await fake_warp("wem2")  # no EVSE -> no command
    assert (await put_text(session, wem, "nfc/inject_tag", {"tag_type": 2, "tag_id": "01:23"}))[
        0
    ] == 404


async def test_check_for_update_and_install_with_vehicle(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")  # fixture has a car connected (IEC B)
    await put(session, fake, "firmware_update/check_for_update")
    assert fake.states["firmware_update/state"]["check_state"] == 0
    assert fake.states["firmware_update/state"]["check_timestamp"] > 1_700_000_000
    resp = await put(
        session, fake, "firmware_update/install_firmware", {"version": "2.13.5+6abe4b1e"}
    )
    assert resp.status == 200  # firmware reports the failure via install_state only
    assert fake.states["firmware_update/install_state"] == {
        "origin": 2,
        "state": InstallState.VEHICLE_CONNECTED,
        "progress": 0,
    }
    status, _ = await put_text(session, fake, "firmware_update/install_firmware", {"version": "x"})
    assert status == 400


async def test_install_progress_without_vehicle(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp2")
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        await put(session, fake, "firmware_update/install_firmware", {"version": "2.13.5+6abe4b1e"})
        seen = []
        while len(seen) < 4:
            msg = await next_msg(ws)
            assert msg["topic"] == "firmware_update/install_state"
            seen.append((msg["payload"]["state"], msg["payload"]["progress"]))
    assert seen == [
        (InstallState.IN_PROGRESS, 0),
        (InstallState.IN_PROGRESS, 50),
        (InstallState.IN_PROGRESS, 100),
        (InstallState.REBOOTING, 100),
    ]


async def test_reboot_closes_ws_with_1001(
    fake_warp: FakeWarpFactory, session: aiohttp.ClientSession
) -> None:
    fake = await fake_warp("warp3")
    async with session.ws_connect(f"{fake.base_url}/ws") as ws:
        await read_dump(ws)
        resp = await put(session, fake, "reboot")
        assert resp.status == 200
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert ws.close_code == 1001
    assert fake.states["info/keep_alive"]["uptime"] < 10_000
    assert fake.reboot_count == 1
