"""Shared fixtures: custom-integration loading and the fake WARP device factory."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import pytest
import pytest_socket

from tests.fake_warp import FakeWarp, load_fixture, patch

FakeWarpFactory = Callable[..., Awaitable[FakeWarp]]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom_components/ in every test."""


@pytest.fixture
async def fake_warp(socket_enabled: None) -> AsyncGenerator[FakeWarpFactory]:
    """Factory: ``await fake_warp("warp3", auth=("user", "pw"), patches={...}, **options)``.

    Servers listen on 127.0.0.1 only and are closed at teardown.
    """
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    started: list[FakeWarp] = []

    async def factory(
        fixture: str | list[dict[str, Any]] = "warp3",
        *,
        auth: tuple[str, str] | None = None,
        patches: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FakeWarp:
        messages = load_fixture(fixture) if isinstance(fixture, str) else fixture
        if patches:
            messages = patch(messages, patches)
        server = FakeWarp(messages, auth=auth, **kwargs)
        await server.start()
        started.append(server)
        return server

    yield factory
    for server in reversed(started):
        await server.close()


@pytest.fixture
def fake_warp_factory(fake_warp: FakeWarpFactory) -> FakeWarpFactory:
    """Alias of ``fake_warp``."""
    return fake_warp
