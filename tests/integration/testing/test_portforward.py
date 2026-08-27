"""Integration tests for portforward utilities requiring real socket operations.

Moved from tests/unit/testing/e2e/test_portforward.py when the unit suite was
hardened with --disable-socket.  These tests exercise functions that bind real
TCP sockets and therefore cannot run as hermetic unit tests.

The code itself moved to ``testing/harness/cluster/_portforward`` with the typed
cluster backend (FND-241); this imports the private helper from where it now
lives, since ``testing/e2e/portforward`` re-exports only the public surface.
"""

import socket
from unittest.mock import patch

import pytest

from application_sdk.testing.harness.cluster._portforward import _find_free_port


@pytest.mark.integration
def test_find_free_port_returns_integer():
    port = _find_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


@pytest.mark.integration
def test_find_free_port_binds_loopback_only():
    """The probe binds loopback, not every interface.

    Asserted on the address the socket is actually bound to rather than on the
    returned port, because ``""`` and ``"127.0.0.1"`` both return a plausible
    port and the difference is invisible in the return value — the same reason
    the ``kube_context`` and ``--context`` pins assert on what was constructed.

    It is the more accurate question to ask: ``kubectl port-forward`` binds its
    local end to loopback, so "free on ``0.0.0.0``" is not the claim this
    function needs to make. It is *not* a fix for a port ``kubectl`` could then
    fail to take — a TCP bind conflicts symmetrically, so the kernel's ephemeral
    choice avoided conflicts under either address. See the docstring on
    :func:`_find_free_port` for that distinction, which matters because the
    plausible mechanism is the wrong one.
    """
    bound: list[tuple[str, int]] = []
    real_bind = socket.socket.bind

    def _record(self: socket.socket, address: tuple[str, int]) -> None:
        bound.append(address)
        return real_bind(self, address)

    with patch.object(socket.socket, "bind", _record):
        port = _find_free_port()

    assert bound == [("127.0.0.1", 0)]
    assert 1024 <= port <= 65535
