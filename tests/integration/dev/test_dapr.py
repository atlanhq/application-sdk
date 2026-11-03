"""Integration tests for dev utilities that require real socket operations.

Moved from tests/unit/dev/test_dapr.py when the unit suite was hardened with
--disable-socket.  These tests exercise functions that bind real TCP sockets
(port-picking helpers) and therefore cannot run as hermetic unit tests.
"""

import pytest

from application_sdk.dev._dapr import _pick_free_port, _pick_free_ports


@pytest.mark.integration
class TestPickFreePort:
    def test_returns_int_in_valid_range(self) -> None:
        port = _pick_free_port()
        assert isinstance(port, int)
        # Ephemeral port range — varies by OS, but always > 1023.
        assert 1024 <= port <= 65535


@pytest.mark.integration
class TestPickFreePorts:
    def test_ports_are_distinct(self) -> None:
        """http and grpc must never be handed the same port: every socket is
        held until all are read."""
        for _ in range(50):
            ports = _pick_free_ports(2)
            assert len(set(ports)) == 2
