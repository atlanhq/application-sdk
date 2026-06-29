"""Integration tests for portforward utilities requiring real socket operations.

Moved from tests/unit/testing/e2e/test_portforward.py when the unit suite was
hardened with --disable-socket.  These tests exercise functions that bind real
TCP sockets and therefore cannot run as hermetic unit tests.
"""

import pytest

from application_sdk.testing.e2e.portforward import _find_free_port


@pytest.mark.integration
def test_find_free_port_returns_integer():
    port = _find_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535
