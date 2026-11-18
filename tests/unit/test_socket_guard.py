"""``loopback_socketpair`` works under pytest-socket's guard; nothing else does.

Windows unit legs depend on this: ``tests/conftest.py`` installs it as
``socket.socketpair`` there so ProactorEventLoop can make its self-pipe while
``--disable-socket`` refuses every socket a test creates (FND-4287). It is
plain socket code, so these tests run, and prove it, on every OS.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator

import pytest
import pytest_socket

from tests.socket_guard import loopback_socketpair


@pytest.fixture
def guarded() -> Iterator[None]:
    """Socket creation refused, as on a CI leg, even when run without the flag."""
    pytest_socket.disable_socket(allow_unix_socket=False)
    try:
        yield
    finally:
        pytest_socket.enable_socket()


def test_guard_refuses_a_test_socket(guarded: None) -> None:
    with pytest.raises(pytest_socket.SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


@pytest.mark.skipif(
    not hasattr(socket, "_fallback_socketpair"),
    reason="CPython exposes the emulated pair on every OS from 3.12; on 3.11 it is Windows-only",
)
def test_cpython_fallback_pair_is_refused_under_the_guard(guarded: None) -> None:
    """What Windows' ProactorEventLoop would hit without the replacement."""
    with pytest.raises(pytest_socket.SocketBlockedError):
        socket._fallback_socketpair()  # type: ignore[attr-defined]  # — CPython's Windows socketpair


def test_loopback_pair_is_built_and_connected_under_the_guard(guarded: None) -> None:
    server, client = loopback_socketpair()
    try:
        client.sendall(b"ping")
        assert server.recv(4) == b"ping"
        assert client.getpeername()[0] == "127.0.0.1"
    finally:
        server.close()
        client.close()


def test_event_loop_self_pipe_works_with_the_loopback_pair(
    guarded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stand-in for a Windows leg: the event loop's self-pipe comes from
    ``socket.socketpair`` while the guard refuses socket creation."""
    monkeypatch.setattr(socket, "socketpair", loopback_socketpair)
    loop = asyncio.SelectorEventLoop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0, result="ran")) == "ran"
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("family", "type_", "proto"),
    [
        pytest.param(socket.AF_UNSPEC, socket.SOCK_STREAM, 0, id="family"),
        pytest.param(socket.AF_INET, socket.SOCK_DGRAM, 0, id="type"),
        pytest.param(socket.AF_INET, socket.SOCK_STREAM, 6, id="proto"),
    ],
)
def test_rejects_what_cpython_fallback_rejects(
    family: int, type_: int, proto: int
) -> None:
    with pytest.raises(ValueError):
        loopback_socketpair(family, type_, proto)
