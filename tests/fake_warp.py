"""Fake Tinkerforge WARP device: WebSocket dump/push plus HTTP command API.

Runs on 127.0.0.1 only. Mirrors firmware quirks (esp32-firmware master):
silent no-op start/stop without a car, `external_enabled_update` forcing 32 A,
install progress on its own topic, optional digest auth and HTTP->HTTPS redirect.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import enum
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, web
from aiohttp.test_utils import TestServer

FIXTURES = Path(__file__).parent / "fixtures"
REALM = "esp32-lib"

# Sentinel for patch(): delete the topic.
REMOVE = object()


class CheckState(enum.IntEnum):
    """firmware_update/state.check_state (firmware_update/Check State.uint8.enum)."""

    IDLE = 0  # also the result "no update / done"
    IN_PROGRESS = 1
    BUSY = 2
    INTERNAL_ERROR = 3
    NO_UPDATE_URL = 4
    NO_CERT = 5
    HTTP_CLIENT_INIT_FAILED = 6
    NO_RESPONSE = 7
    DOWNLOAD_ERROR = 8
    VERSION_MALFORMED = 9
    INDEX_MALFORMED = 10
    ABORTED = 11
    DOWNLOAD_SHORT_READ = 12
    NOT_SUPPORTED = 13


class InstallState(enum.IntEnum):
    """firmware_update/install_state.state (firmware_update/Install State.uint8.enum)."""

    IDLE = 0
    IN_PROGRESS = 1
    BUSY = 2
    INTERNAL_ERROR = 3
    NO_UPDATE_URL = 4
    NO_CERT = 5
    HTTP_CLIENT_INIT_FAILED = 6
    NO_RESPONSE = 7
    DOWNLOAD_ERROR = 8
    VERSION_MALFORMED = 9
    FIRMWARE_TOO_SMALL = 10
    FLASH_BEGIN_FAILED = 11
    FLASH_SHORT_WRITE = 12
    FLASH_APPLY_FAILED = 13
    SIGNATURE_BEGIN_FAILED = 14
    SIGNATURE_UPDATE_FAILED = 15
    SIGNATURE_VERIFY_FAILED = 16
    VEHICLE_CONNECTED = 17
    FIRMWARE_INFO_TOO_BIG = 18
    FIRMWARE_INFO_MISSING = 19
    FIRMWARE_INFO_CORRUPTED = 20
    WRONG_FIRMWARE_TYPE = 21
    DOWNGRADE = 22
    ABORTED = 23
    REBOOTING = 24
    DOWNLOAD_SHORT_READ = 25
    NOT_SUPPORTED = 26
    FIRMWARE_SIZE_UNKNOWN = 27
    RECEIVE_ERROR = 28
    SIGNATURE_INFO_CORRUPTED = 29
    APPLICATION_CORRUPTED = 30
    SECURE_BOOT_SIGNATURE_GENERATION_FAILED = 31


# install_state.origin (Install Origin.uint8.enum): None 0, Flash Firmware 1, Install Firmware 2
INSTALL_ORIGIN_INSTALL_FIRMWARE = 2

# evse_common.h
SLOT_AUTOSTART_BUTTON = 4
SLOT_EXTERNAL = 8
NFC_INJECTED_SLOT = 8  # TAG_LIST_LENGTH - 1

# power_manager.h EXTERNAL_CONTROL_STATE_*
EXTERNAL_CONTROL_AVAILABLE = 0
EXTERNAL_CONTROL_DISABLED = 1
EXTERNAL_CONTROL_UNAVAILABLE = 2
EXTERNAL_CONTROL_SWITCHING = 3

# charge_limits.cpp map_duration(): override_duration value -> minutes
DURATION_MINUTES = {1: 15, 2: 30, 3: 45, 4: 60, 5: 120, 6: 180, 7: 240, 8: 360, 9: 480, 10: 720}

_TAG_ID_RE = re.compile(r"^([0-9A-F]{2}(:[0-9A-F]{2})*)?$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-beta\.\d+)?(\+[0-9a-f]+)?$")


def load_fixture(name: str) -> list[dict[str, Any]]:
    """Read tests/fixtures/<name>.jsonl as a list of {"topic", "payload"}."""
    path = FIXTURES / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def patch(states: list[dict[str, Any]], patches: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a copy of a fixture with topic patches applied.

    dict patch + dict payload -> shallow merge; anything else replaces; REMOVE deletes;
    unknown topics are appended.
    """
    out = copy.deepcopy(states)
    index = {m["topic"]: m for m in out}
    for topic, value in patches.items():
        if value is REMOVE:
            out = [m for m in out if m["topic"] != topic]
            index.pop(topic, None)
        elif topic in index:
            cur = index[topic]
            if isinstance(value, dict) and isinstance(cur["payload"], dict):
                cur["payload"] = {**cur["payload"], **value}
            else:
                cur["payload"] = copy.deepcopy(value)
        else:
            msg = {"topic": topic, "payload": copy.deepcopy(value)}
            out.append(msg)
            index[topic] = msg
    return out


class HttpError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


@dataclass(eq=False)
class _Client:
    ws: web.WebSocketResponse
    request: web.Request
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    live: bool = False  # dump finished; until then pushes are queued
    pending: list[str] = field(default_factory=list)


def _wire(topic: str, payload: Any) -> str:
    return json.dumps({"topic": topic, "payload": payload}) + "\n"


def _kind_ok(value: Any, ref: Any) -> bool:
    """Loose firmware-like type check of an update value against the current value."""
    if ref is None:
        return True
    if isinstance(ref, bool):
        return isinstance(value, bool)
    if isinstance(ref, int | float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, type(ref))


class FakeWarp:
    """In-process WARP device on 127.0.0.1.

    Mutable options (read at request time): ``dump_delay``, ``hold_dump``,
    ``reject_ws_status``, ``redirect_to``, ``max_clients``, ``vehicle_charges``,
    ``check_result``, ``install_step_delay``, ``install_reboots``.
    """

    def __init__(
        self,
        fixture: str | list[dict[str, Any]],
        *,
        auth: tuple[str, str] | None = None,
        frame_size: int = 3,
        dump_delay: float = 0.0,
        hold_dump: bool = False,
        reject_ws_status: int | None = None,
        redirect_to: str | None = None,
        max_clients: int | None = None,
        keep_alive_interval: float | None = None,
        vehicle_charges: bool = True,
        check_result: CheckState = CheckState.IDLE,
        install_step_delay: float = 0.0,
        install_reboots: bool = False,
    ) -> None:
        messages = load_fixture(fixture) if isinstance(fixture, str) else fixture
        self.states: dict[str, Any] = {m["topic"]: copy.deepcopy(m["payload"]) for m in messages}
        self.auth = auth
        self.frame_size = frame_size
        self.dump_delay = dump_delay
        self.hold_dump = hold_dump
        self.reject_ws_status = reject_ws_status
        self.redirect_to = redirect_to
        self.max_clients = max_clients
        self.keep_alive_interval = keep_alive_interval
        self.vehicle_charges = vehicle_charges  # plugged-in car charges whenever allowed >= 6 A
        self.check_result = check_result
        self.install_step_delay = install_step_delay
        self.install_reboots = install_reboots

        self.puts: list[tuple[str, Any]] = []  # (path without leading "/", parsed JSON body)
        self.requests: list[tuple[str, str]] = []  # (method, path) of every non-ws HTTP request
        self.ws_connect_count = 0
        self.kick_count = 0
        self.reboot_count = 0

        self._clients: list[_Client] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._server: TestServer | None = None
        self._commands = {
            "reboot": self._cmd_reboot,
            "evse/start_charging": self._cmd_start_charging,
            "evse/stop_charging": self._cmd_stop_charging,
            "charge_limits/override_duration": self._cmd_override_duration,
            "charge_limits/override_energy": self._cmd_override_energy,
            "charge_limits/override_soc": self._cmd_override_soc,
            "charge_limits/restart": self._cmd_limits_restart,
            "ev/inject_soc": self._cmd_inject_soc,
            "firmware_update/check_for_update": self._cmd_check_for_update,
            "firmware_update/install_firmware": self._cmd_install_firmware,
            "nfc/inject_tag": self._cmd_inject_tag,
        }
        self._updates = {
            "evse/external_current": self._upd_external_current,
            "evse/external_enabled": self._upd_external_enabled,
            "evse/external_clear_on_disconnect": self._upd_external_clear_on_disconnect,
            "evse/auto_start_charging": self._upd_auto_start,
            "evse/charge_mode": self._upd_evse_charge_mode,
            "power_manager/charge_mode": self._upd_pm_charge_mode,
            "power_manager/external_control": self._upd_external_control,
        }

        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/{path:.+}", self._http_get)
        app.router.add_route("PUT", "/{path:.+}", self._http_write)
        app.router.add_route("POST", "/{path:.+}", self._http_write)
        app.on_shutdown.append(self._on_shutdown)
        self._app = app

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._server = TestServer(self._app, host="127.0.0.1")
        await self._server.start_server()
        if self.keep_alive_interval:
            self._spawn(self._keep_alive_loop())

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            await self._server.close()
            self._server = None

    async def _on_shutdown(self, _app: web.Application) -> None:
        await self.drop_clients(1001)

    @property
    def base_url(self) -> str:
        assert self._server is not None, "FakeWarp not started"
        return f"http://127.0.0.1:{self._server.port}"

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ push / state

    async def push(self, topic: str, payload: Any) -> None:
        """Set a state and send it to all connected ws clients."""
        self.states[topic] = payload
        text = _wire(topic, payload)
        for client in list(self._clients):
            await self._deliver(client, text)

    async def _publish(self, topic: str, payload: Any) -> None:
        """Like push() but only when the value changed (the device pushes on change)."""
        if topic in self.states and self.states[topic] == payload:
            return
        await self.push(topic, payload)

    async def _merge(self, topic: str, **fields: Any) -> None:
        await self._publish(topic, {**self.states[topic], **fields})

    async def _deliver(self, client: _Client, text: str) -> None:
        async with client.lock:
            if not client.live:
                client.pending.append(text)
                return
            await self._send(client, text)

    async def _send(self, client: _Client, text: str) -> None:
        try:
            await client.ws.send_str(text)
        except ConnectionResetError, RuntimeError:
            self._remove(client)

    def _remove(self, client: _Client) -> None:
        if client in self._clients:
            self._clients.remove(client)

    async def drop_clients(self, code: int = 1006) -> None:
        """Close all ws clients. 1001: close frame (reboot); 1006: abort the TCP connection
        without a close frame (client sees an abnormal closure); other codes: close frame."""
        clients, self._clients = self._clients, []
        await asyncio.gather(*(self._close_client(c, code) for c in clients))

    async def _close_client(self, client: _Client, code: int) -> None:
        if code == WSCloseCode.ABNORMAL_CLOSURE:
            if client.request.transport is not None:
                client.request.transport.abort()
            return
        message = b"ESP reboot" if code == WSCloseCode.GOING_AWAY else b""
        with contextlib.suppress(Exception):
            await client.ws.close(code=code, message=message)

    async def _keep_alive_loop(self) -> None:
        assert self.keep_alive_interval
        while True:
            await asyncio.sleep(self.keep_alive_interval)
            uptime = self.states.get("info/keep_alive", {}).get("uptime", 0)
            await self.push(
                "info/keep_alive", {"uptime": uptime + int(self.keep_alive_interval * 1000)}
            )

    # ------------------------------------------------------------------ test helpers

    async def plug_in(self) -> None:
        """Connect a car (IEC B/C, per slots) on an EVSE device."""
        await self._merge("evse/state", iec61851_state=1)
        await self._recompute_evse()

    async def unplug(self) -> None:
        """Disconnect the car and apply clear_on_disconnect slot resets."""
        slots = copy.deepcopy(self.states["evse/slots"])
        if slots[SLOT_AUTOSTART_BUTTON]["clear_on_disconnect"]:
            slots[SLOT_AUTOSTART_BUTTON]["max_current"] = 0
        if slots[SLOT_EXTERNAL]["clear_on_disconnect"]:
            default = self.states.get("evse/external_defaults", {}).get("current", 32000)
            slots[SLOT_EXTERNAL]["max_current"] = default
            await self._publish("evse/external_current", {"current": default})
        await self._publish("evse/slots", slots)
        await self._merge("evse/state", iec61851_state=0, charger_state=0, contactor_state=0)
        await self._recompute_evse()

    async def reboot(self) -> None:
        """Emulate an ESP reboot: boot_id++, uptime reset, ws closed with 1001."""
        self.reboot_count += 1
        if "event_log/boot_id" in self.states:
            self.states["event_log/boot_id"] = {
                "boot_id": self.states["event_log/boot_id"]["boot_id"] + 1
            }
        if "info/keep_alive" in self.states:
            self.states["info/keep_alive"] = {"uptime": 1000}
        await self.drop_clients(1001)

    # ------------------------------------------------------------------ HTTP

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.redirect_to:
            raise web.HTTPTemporaryRedirect(self.redirect_to.rstrip("/") + request.path_qs)
        if self.auth is not None and not self._digest_ok(request):
            raise web.HTTPUnauthorized(
                text="Unauthorized",
                headers={
                    "WWW-Authenticate": (
                        f'Digest realm="{REALM}", qop="auth", '
                        f'nonce="{secrets.token_hex(16)}", opaque="{secrets.token_hex(16)}"'
                    )
                },
            )
        if request.path != "/ws":
            self.requests.append((request.method, request.path))
        return await handler(request)

    def _digest_ok(self, request: web.Request) -> bool:
        """Firmware-like check: recompute only, no nonce/opaque/uri/nc validation."""
        assert self.auth is not None
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        params = {
            k: v[1:-1] if v.startswith('"') else v
            for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', header[7:])
        }
        try:
            user, password = self.auth
            if params["username"] != user:
                return False
            ha1 = _md5(f"{user}:{REALM}:{password}")
            ha2 = _md5(f"{request.method}:{params['uri']}")
            if params.get("qop") == "auth":
                expected = _md5(
                    f"{ha1}:{params['nonce']}:{params['nc']}:{params['cnonce']}:auth:{ha2}"
                )
            else:
                expected = _md5(f"{ha1}:{params['nonce']}:{ha2}")
        except KeyError:
            return False
        return secrets.compare_digest(expected, params.get("response", ""))

    async def _http_get(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"]
        if path == "credential_check":
            return web.Response(text="Credentials okay")
        if path == "login_state":
            return web.Response(text="Logged in")
        if path in self.states:
            return web.json_response(self.states[path])
        return web.Response(status=404, text="Nothing matches the given URI.")

    async def _http_write(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"]
        raw = await request.read()
        try:
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            return web.Response(status=400, text="Failed to parse JSON")
        self.puts.append((path, body))
        try:
            await self._apply(path, body)
        except HttpError as err:
            return web.Response(status=err.status, text=err.text)
        return web.Response()

    async def _apply(self, path: str, body: Any) -> None:
        if path in self._commands:
            await self._commands[path](body)
            return
        base = path.removesuffix("_update")
        if base not in self.states:
            if path not in self.states:
                raise HttpError(404, "Nothing matches the given URI.")
            base = path
        body = self._wrap(base, body)
        if base in self._updates:
            await self._updates[base](body)
            return
        await self._generic_update(base, body)

    def _wrap(self, topic: str, body: Any) -> Any:
        """Single-member shorthand: bare value for a one-member object."""
        cur = self.states.get(topic)
        if isinstance(cur, dict) and len(cur) == 1 and not isinstance(body, dict | type(None)):
            return {next(iter(cur)): body}
        return body

    async def _generic_update(self, topic: str, body: Any) -> None:
        cur = self.states[topic]
        if not isinstance(cur, dict) or not isinstance(body, dict):
            raise HttpError(400, "Expected a JSON object")
        for key, value in body.items():
            if key not in cur:
                raise HttpError(400, f"Unknown key '{key}'")
            if not _kind_ok(value, cur[key]):
                raise HttpError(400, f"Type mismatch for key '{key}'")
        await self._publish(topic, {**cur, **body})

    # ------------------------------------------------------------------ payload helpers

    @staticmethod
    def _one(body: Any, key: str, kind: str) -> Any:
        """Extract a required member (bare value accepted); kind: bool|int|float|str."""
        if not isinstance(body, dict):
            body = {key: body}
        if key not in body:
            raise HttpError(400, f"Key '{key}' not found")
        value = body[key]
        ok = {
            "bool": isinstance(value, bool),
            "int": isinstance(value, int) and not isinstance(value, bool),
            "float": isinstance(value, int | float) and not isinstance(value, bool),
            "str": isinstance(value, str),
        }[kind]
        if not ok:
            raise HttpError(400, f"Type mismatch for key '{key}'")
        return value

    def _require(self, *topics: str) -> None:
        if any(t not in self.states for t in topics):
            raise HttpError(404, "Nothing matches the given URI.")

    # ------------------------------------------------------------------ EVSE model

    async def _set_slot(self, index: int, **fields: Any) -> None:
        slots = copy.deepcopy(self.states["evse/slots"])
        slots[index].update(fields)
        await self._publish("evse/slots", slots)

    async def _recompute_evse(self) -> None:
        """allowed current = min over active slots; derive IEC/charger state for a plugged car."""
        if "evse/slots" not in self.states or "evse/state" not in self.states:
            return
        active = [s["max_current"] for s in self.states["evse/slots"] if s["active"]]
        allowed = min(active, default=0)
        fields: dict[str, Any] = {"allowed_charging_current": allowed}
        iec = self.states["evse/state"]["iec61851_state"]
        if iec in (1, 2, 3):
            if allowed < 6000:
                fields.update(iec61851_state=1, charger_state=1, contactor_state=0)
            elif self.vehicle_charges:
                fields.update(iec61851_state=2, charger_state=3, contactor_state=3)
            else:
                fields.update(iec61851_state=1, charger_state=2, contactor_state=0)
        await self._merge("evse/state", **fields)

    def _vehicle_connected(self) -> bool:
        return self.states.get("evse/state", {}).get("iec61851_state", 0) != 0

    # ------------------------------------------------------------------ EVSE commands

    async def _cmd_start_charging(self, _body: Any) -> None:
        self._require("evse/state", "evse/slots")
        if not self._vehicle_connected():
            return  # firmware: silent no-op in IEC state A
        await self._set_slot(SLOT_AUTOSTART_BUTTON, max_current=32000)
        await self._recompute_evse()

    async def _cmd_stop_charging(self, _body: Any) -> None:
        self._require("evse/state", "evse/slots")
        if not self._vehicle_connected():
            return
        await self._set_slot(SLOT_AUTOSTART_BUTTON, max_current=0)
        await self._recompute_evse()

    async def _upd_external_current(self, body: Any) -> None:
        # Out-of-range values are applied too (firmware only skips the watchdog reset).
        current = self._one(body, "current", "int")
        if not 0 <= current <= 0xFFFF:
            raise HttpError(400, "Integer value out of range")
        await self._publish("evse/external_current", {"current": current})
        await self._set_slot(SLOT_EXTERNAL, max_current=current, active=True)
        await self._recompute_evse()

    async def _upd_external_enabled(self, body: Any) -> None:
        enabled = self._one(body, "enabled", "bool")
        if enabled == self.states["evse/external_enabled"]["enabled"]:
            return
        # Firmware: "We don't allow to disable the external slot anymore" -> force 32 A, active.
        await self._set_slot(
            SLOT_EXTERNAL, max_current=32000, active=True, clear_on_disconnect=False
        )
        await self._publish("evse/external_enabled", {"enabled": True})
        await self._publish("evse/external_current", {"current": 32000})
        await self._publish(
            "evse/external_defaults", {"current": 32000, "clear_on_disconnect": False}
        )
        await self._publish("evse/external_clear_on_disconnect", {"clear_on_disconnect": False})
        await self._recompute_evse()

    async def _upd_external_clear_on_disconnect(self, body: Any) -> None:
        value = self._one(body, "clear_on_disconnect", "bool")
        await self._publish("evse/external_clear_on_disconnect", {"clear_on_disconnect": value})
        await self._set_slot(SLOT_EXTERNAL, clear_on_disconnect=value)

    async def _upd_auto_start(self, body: Any) -> None:
        enable = self._one(body, "auto_start_charging", "bool")
        await self._publish("evse/auto_start_charging", {"auto_start_charging": enable})
        await self._set_slot(SLOT_AUTOSTART_BUTTON, clear_on_disconnect=not enable)
        iec = self.states["evse/state"]["iec61851_state"]
        if enable:
            await self._set_slot(SLOT_AUTOSTART_BUTTON, max_current=32000)
        elif iec not in (2, 3):
            await self._set_slot(SLOT_AUTOSTART_BUTTON, max_current=0)
        await self._recompute_evse()

    async def _upd_evse_charge_mode(self, body: Any) -> None:
        mode = self._one(body, "mode", "int")
        if not self.states.get("evse/management_enabled", {}).get("enabled"):
            raise HttpError(400, "Charge management is not enabled")
        if mode not in self.states.get("evse/supported_charge_modes", []):
            raise HttpError(400, "Charge mode not supported")
        await self._publish("evse/charge_mode", {"mode": mode})

    async def _upd_pm_charge_mode(self, body: Any) -> None:
        mode = self._one(body, "mode", "int")
        if mode not in self.states.get("charge_manager/supported_charge_modes", []):
            raise HttpError(400, "Charge mode not supported")
        await self._publish("power_manager/charge_mode", {"mode": mode})

    async def _upd_external_control(self, body: Any) -> None:
        if "phase_switch" not in self.states.get("info/features", []):
            raise HttpError(404, "Nothing matches the given URI.")
        phases = self._one(body, "phases_wanted", "int")
        if phases not in (1, 3):
            raise HttpError(400, "Invalid value for phases_wanted! Only 1 or 3 are allowed.")
        state = self.states.get("power_manager/state", {}).get("external_control")
        if state == EXTERNAL_CONTROL_DISABLED:
            raise HttpError(
                400,
                "Ignoring external control phase change request: External control is not enabled.",
            )
        if state == EXTERNAL_CONTROL_UNAVAILABLE:
            raise HttpError(
                400,
                "Ignoring external control phase change request: Phase switching in error state.",
            )
        await self._publish("power_manager/external_control", {"phases_wanted": phases})
        if "evse/low_level_state" in self.states:
            await self._merge(
                "evse/low_level_state", phases_requested=phases, phases_current=phases
            )
        if "power_manager/low_level_state" in self.states:
            await self._merge("power_manager/low_level_state", is_3phase=phases == 3)

    # ------------------------------------------------------------------ misc commands

    async def _cmd_reboot(self, _body: Any) -> None:
        # Respond first, then drop the sockets like the device does.
        self._spawn(self._delayed_reboot())

    async def _delayed_reboot(self) -> None:
        await asyncio.sleep(0.05)
        await self.reboot()

    async def _cmd_override_duration(self, body: Any) -> None:
        self._require("charge_limits/active_limits", "charge_limits/state")
        duration = self._one(body, "duration", "int")
        if not 0 <= duration <= 10:
            raise HttpError(400, "Integer value out of range")
        await self._merge("charge_limits/active_limits", duration=duration)
        start = self.states["charge_limits/state"]["start_timestamp_ms"]
        target = 0 if duration == 0 else start + DURATION_MINUTES[duration] * 60_000
        await self._merge("charge_limits/state", target_timestamp_ms=target)

    async def _cmd_override_energy(self, body: Any) -> None:
        self._require("charge_limits/active_limits", "charge_limits/state")
        energy_wh = self._one(body, "energy_wh", "int")
        if not 0 <= energy_wh <= 0xFFFFFFFF:
            raise HttpError(400, "Integer value out of range")
        await self._merge("charge_limits/active_limits", energy_wh=energy_wh)
        start = self.states["charge_limits/state"]["start_energy_kwh"]
        target = None if energy_wh == 0 or start is None else start + energy_wh / 1000
        await self._merge("charge_limits/state", target_energy_kwh=target)

    async def _cmd_override_soc(self, body: Any) -> None:
        if "soc_pct" not in self.states.get("charge_limits/active_limits", {}):
            raise HttpError(404, "Nothing matches the given URI.")
        soc = self._one(body, "soc_pct", "int")
        if not 0 <= soc <= 100:
            raise HttpError(400, "Integer value out of range")
        await self._merge("charge_limits/active_limits", soc_pct=soc)
        await self._merge("charge_limits/state", target_soc_pct=soc)

    async def _cmd_limits_restart(self, _body: Any) -> None:
        self._require("charge_limits/active_limits", "charge_limits/state")
        if self.states.get("charge_tracker/current_charge", {}).get("user_id") == -1:
            return
        active = self.states["charge_limits/active_limits"]
        start = self.states.get("evse/low_level_state", {}).get("uptime", 0)
        fields: dict[str, Any] = {
            "start_timestamp_ms": start,
            "target_timestamp_ms": (
                start + DURATION_MINUTES[active["duration"]] * 60_000 if active["duration"] else 0
            ),
        }
        energy = self.states.get("meter/values", {}).get("energy_abs")
        if energy is not None:
            fields["start_energy_kwh"] = energy
            fields["target_energy_kwh"] = (
                energy + active["energy_wh"] / 1000 if active["energy_wh"] else None
            )
        if "soc_pct" in active:
            fields["target_soc_pct"] = active["soc_pct"]
        await self._merge("charge_limits/state", **fields)

    async def _cmd_inject_soc(self, body: Any) -> None:
        self._require("ev/state")
        soc = float(self._one(body, "soc", "float"))
        if not 0.0 <= soc <= 100.0:
            raise HttpError(400, "SoC must be between 0 and 100")
        await self._merge("ev/state", soc=soc)
        for slot in range(16):  # ISO 15118 vehicle meter (class 9) mirrors the SoC (value id 387)
            cfg = self.states.get(f"meters/{slot}/config")
            ids = self.states.get(f"meters/{slot}/value_ids")
            if cfg and cfg[0] == 9 and ids and 387 in ids:
                values = list(self.states[f"meters/{slot}/values"])
                values[ids.index(387)] = soc
                await self._publish(f"meters/{slot}/values", values)
                await self._publish(f"meters/{slot}/state", {"source": 1})

    async def _cmd_inject_tag(self, body: Any) -> None:
        # The firmware only registers this command on builds with an EVSE.
        self._require("nfc/seen_tags", "evse/state")
        tag_type = self._one(body, "tag_type", "int")
        tag_id = self._one(body, "tag_id", "str").upper()
        if not 0 <= tag_type <= 6:
            raise HttpError(400, "Integer value out of range")
        if not _TAG_ID_RE.match(tag_id):
            raise HttpError(
                400,
                "Tag ID has unexpected format. Expected uppercase hex bytes separated by colons."
                ' For example "01:23:AB:3D".',
            )
        tags = copy.deepcopy(self.states["nfc/seen_tags"])
        tags[NFC_INJECTED_SLOT] = {"tag_type": tag_type, "tag_id": tag_id, "last_seen": 0}
        await self._publish("nfc/seen_tags", tags)

    async def _cmd_check_for_update(self, _body: Any) -> None:
        self._require("firmware_update/state")
        await self._publish(
            "firmware_update/install_state",
            {"origin": 0, "state": InstallState.IDLE, "progress": 0},
        )
        await self._merge(
            "firmware_update/state",
            check_timestamp=int(time.time()),
            check_state=CheckState.IN_PROGRESS,
        )
        await self._merge("firmware_update/state", check_state=self.check_result)

    async def _cmd_install_firmware(self, body: Any) -> None:
        self._require("firmware_update/install_state")
        version = self._one(body, "version", "str")
        if not _VERSION_RE.match(version):
            raise HttpError(400, "Version is malformed")
        if self._vehicle_connected():
            # Firmware answers 200 and reports the error on install_state.
            await self._set_install(InstallState.VEHICLE_CONNECTED, 0)
            return
        self._spawn(self._install_sequence())

    async def _set_install(self, state: InstallState, progress: int) -> None:
        await self.push(
            "firmware_update/install_state",
            {"origin": INSTALL_ORIGIN_INSTALL_FIRMWARE, "state": int(state), "progress": progress},
        )

    async def _install_sequence(self) -> None:
        for progress in (0, 50, 100):
            await self._set_install(InstallState.IN_PROGRESS, progress)
            await asyncio.sleep(self.install_step_delay)
        await self._set_install(InstallState.REBOOTING, 100)
        if self.install_reboots:
            await asyncio.sleep(self.install_step_delay)
            await self.reboot()

    # ------------------------------------------------------------------ WebSocket

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        if self.reject_ws_status:
            return web.Response(status=self.reject_ws_status, text="rejected")
        ws = web.WebSocketResponse(heartbeat=None, timeout=2.0)
        await ws.prepare(request)
        self.ws_connect_count += 1
        client = _Client(ws, request)
        if self.max_clients is not None:
            while len(self._clients) >= self.max_clients:
                # Firmware kicks the client that ponged least recently; approximated by the oldest.
                victim = self._clients.pop(0)
                self.kick_count += 1
                await self._close_client(victim, WSCloseCode.ABNORMAL_CLOSURE)
        self._clients.append(client)
        writer = asyncio.get_running_loop().create_task(self._dump(client))
        try:
            async for _msg in ws:  # receive-only protocol; just detect close
                pass
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer
            self._remove(client)
        return ws

    async def _dump(self, client: _Client) -> None:
        messages = [_wire(t, p) for t, p in list(self.states.items())]
        size = max(1, self.frame_size)
        frames = ["".join(messages[i : i + size]) for i in range(0, len(messages), size)] or [""]
        if not self.hold_dump:
            frames[-1] += "\n"  # end marker: "\n\n"
        try:
            for frame in frames:
                await client.ws.send_str(frame)
                if self.dump_delay:
                    await asyncio.sleep(self.dump_delay)
            if self.hold_dump:
                await asyncio.Event().wait()
            async with client.lock:
                while client.pending:
                    await client.ws.send_str(client.pending.pop(0))
                client.live = True
        except ConnectionResetError, RuntimeError:
            self._remove(client)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()
