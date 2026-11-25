"""Let asyncio's self-pipe survive ``--disable-socket`` on Windows.

pytest-socket's ``--disable-socket`` replaces ``socket.socket`` for the
duration of each test, so no socket can be created. On Linux and macOS the
event loop's self-pipe is a native AF_UNIX ``socket.socketpair()``, which
``--allow-unix-socket`` lets through. Windows has no native ``socketpair``:
CPython emulates it by building two AF_INET sockets on loopback through the
guarded ``socket.socket``, so every new ``ProactorEventLoop`` would fail.

``--allow-hosts`` is not a substitute. It only patches ``socket.connect``,
and asyncio's Proactor connects through ``ConnectEx`` while UDP sends
through ``sendto``, so neither is guarded by it (FND-4287).

:func:`loopback_socketpair` builds the same pair from the socket class this
module captured at import, before any test runs, so the guard still refuses
every socket a test creates itself. ``tests/conftest.py`` installs it as
``socket.socketpair`` on Windows only.
"""

from __future__ import annotations

import socket

#: The real class, captured when ``tests/conftest.py`` imports this module at
#: collection time. pytest-socket swaps ``socket.socket`` per test, in its
#: ``pytest_runtest_setup``, so this is never the guarded one.
_REAL_SOCKET = socket.socket


def loopback_socketpair(
    family: int = socket.AF_INET,
    type: int = socket.SOCK_STREAM,  # noqa: A002 — mirrors socket.socketpair's signature
    proto: int = 0,
) -> tuple[socket.socket, socket.socket]:
    """Return a connected pair of loopback TCP sockets.

    Same contract as CPython's ``socket._fallback_socketpair``: AF_INET or
    AF_INET6 stream sockets only, and the pair is checked to be connected to
    each other, so a stray connection to the ephemeral port is rejected.
    """
    if family == socket.AF_INET:
        host = "127.0.0.1"
    elif family == socket.AF_INET6:
        host = "::1"
    else:
        raise ValueError(
            "Only AF_INET and AF_INET6 socket address families are supported"
        )
    if type != socket.SOCK_STREAM:
        raise ValueError("Only SOCK_STREAM socket type is supported")
    if proto != 0:
        raise ValueError("Only protocol zero is supported")

    listener = _REAL_SOCKET(family, type, proto)
    try:
        listener.bind((host, 0))
        listener.listen()
        client = _REAL_SOCKET(family, type, proto)
        try:
            client.connect(listener.getsockname()[:2])
            # Not accept(): it wraps the fd through the module-level
            # ``socket`` name, which is the guarded class during a test.
            fd, _ = listener._accept()
            server = _REAL_SOCKET(family, type, proto, fileno=fd)
        except BaseException:
            client.close()
            raise
    finally:
        listener.close()

    if (
        server.getsockname()[:2] != client.getpeername()[:2]
        or client.getsockname()[:2] != server.getpeername()[:2]
    ):
        server.close()
        client.close()
        raise ConnectionError("Unexpected peer connection")
    return server, client
