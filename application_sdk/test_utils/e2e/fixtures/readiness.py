"""Readiness check strategies for data source fixtures.

The SDK provides a generic TCP check. Apps that need database-specific
checks (e.g. verifying seed data loaded) should subclass
YamlContainerizedFixture and override is_ready().
"""

import socket


def check_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if a TCP connection can be established to host:port.

    Works for any service — databases, message brokers, custom servers.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False
