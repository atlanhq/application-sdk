"""Readiness check strategies for data source fixtures.

The SDK provides a generic TCP check. Apps that need database-specific
checks (e.g. verifying seed data loaded) should subclass
YamlContainerizedFixture and override is_ready().
"""

import socket


def check_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if a TCP connection can be established and held by host:port.

    A simple ``connect()`` can succeed even when only Docker's port proxy is
    listening (before the actual service is ready inside the container).
    After connecting, this function attempts a 1-byte ``recv()`` to
    distinguish three cases:

    * **Data received** — the service sent a greeting (e.g. postgres), ready.
    * **recv() times out** — the service holds the connection open waiting
      for the client protocol, ready.
    * **Empty bytes / reset** — Docker's proxy accepted but nothing is
      listening inside the container yet, not ready.

    Works for any service — databases, message brokers, custom servers.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(1.0)
            try:
                data = sock.recv(1)
                return len(data) > 0
            except socket.timeout:
                return True
            except (ConnectionResetError, BrokenPipeError):
                return False
    except (OSError, ConnectionRefusedError):
        return False
