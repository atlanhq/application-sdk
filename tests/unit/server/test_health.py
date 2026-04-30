"""Tests for application_sdk.server.health.

Covers:
- WorkerHealthServer HTTP endpoints: /health, /ready, /live
- 404 and 405 responses
- HealthStatus.to_dict()
- set_temporal_client / record_activity
- Context manager usage
"""

import asyncio
import contextlib
import json
import socket

import pytest

from application_sdk.server.health import (
    HealthStatus,
    WorkerHealthServer,
    run_health_server,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _http_request(
    host: str, port: int, method: str, path: str
) -> tuple[int, dict]:
    """Make a raw HTTP request and return (status_code, body_dict)."""
    reader, writer = await asyncio.open_connection(host, port)
    request = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()

    # Read until EOF (server sends Connection: close)
    chunks = []
    while True:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        if not chunk:
            break
        chunks.append(chunk)

    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    response_text = b"".join(chunks).decode("utf-8")
    # Parse status code from first line
    status_line = response_text.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1])

    # Parse body (after double CRLF)
    body_str = response_text.split("\r\n\r\n", 1)[1]
    body = json.loads(body_str)
    return status_code, body


# ---------------------------------------------------------------------------
# HealthStatus
# ---------------------------------------------------------------------------


class TestHealthStatus:
    def test_to_dict_healthy(self):
        status = HealthStatus(healthy=True, message="OK")
        d = status.to_dict()
        assert d["healthy"] is True
        assert d["message"] == "OK"
        assert "checked_at" in d

    def test_to_dict_unhealthy(self):
        status = HealthStatus(healthy=False, message="Not ready")
        d = status.to_dict()
        assert d["healthy"] is False

    def test_to_dict_with_details(self):
        status = HealthStatus(healthy=True, details={"key": "value"})
        d = status.to_dict()
        assert d["details"]["key"] == "value"


# ---------------------------------------------------------------------------
# WorkerHealthServer
# ---------------------------------------------------------------------------


class TestWorkerHealthServer:
    @pytest.mark.asyncio
    async def test_health_endpoint_returns_200(self):
        port = _find_free_port()
        async with WorkerHealthServer(host="127.0.0.1", port=port):
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/health")
        assert status_code == 200
        assert body["healthy"] is True

    @pytest.mark.asyncio
    async def test_ready_returns_503_without_client(self):
        port = _find_free_port()
        async with WorkerHealthServer(host="127.0.0.1", port=port):
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/ready")
        assert status_code == 503
        assert body["healthy"] is False

    @pytest.mark.asyncio
    async def test_ready_returns_200_with_client(self):
        port = _find_free_port()
        server = WorkerHealthServer(host="127.0.0.1", port=port)

        class FakeClient:
            @property
            def identity(self):
                return "test-worker"

        server.set_temporal_client(FakeClient())
        async with server:
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/ready")
        assert status_code == 200
        assert body["healthy"] is True
        assert body["details"]["identity"] == "test-worker"

    @pytest.mark.asyncio
    async def test_live_endpoint_returns_200(self):
        port = _find_free_port()
        async with WorkerHealthServer(host="127.0.0.1", port=port):
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/live")
        assert status_code == 200
        assert body["healthy"] is True

    @pytest.mark.asyncio
    async def test_404_for_unknown_path(self):
        port = _find_free_port()
        async with WorkerHealthServer(host="127.0.0.1", port=port):
            status_code, body = await _http_request(
                "127.0.0.1", port, "GET", "/unknown"
            )
        assert status_code == 404

    @pytest.mark.asyncio
    async def test_405_for_post_method(self):
        port = _find_free_port()
        async with WorkerHealthServer(host="127.0.0.1", port=port):
            status_code, body = await _http_request(
                "127.0.0.1", port, "POST", "/health"
            )
        assert status_code == 405

    @pytest.mark.asyncio
    async def test_manual_start_stop(self):
        port = _find_free_port()
        server = WorkerHealthServer(host="127.0.0.1", port=port)
        await server.start()
        try:
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/health")
            assert status_code == 200
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_record_activity_updates_last_activity(self):
        port = _find_free_port()
        server = WorkerHealthServer(host="127.0.0.1", port=port)
        assert server._last_activity is None
        server.record_activity()
        assert server._last_activity is not None

    @pytest.mark.asyncio
    async def test_request_count_increments(self):
        port = _find_free_port()
        async with WorkerHealthServer(host="127.0.0.1", port=port) as server:
            assert server._request_count == 0
            await _http_request("127.0.0.1", port, "GET", "/health")
            await _http_request("127.0.0.1", port, "GET", "/live")
            assert server._request_count == 2


# ---------------------------------------------------------------------------
# run_health_server convenience function
# ---------------------------------------------------------------------------


class TestRunHealthServer:
    @pytest.mark.asyncio
    async def test_run_health_server(self):
        port = _find_free_port()
        server = await run_health_server(host="127.0.0.1", port=port)
        try:
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/health")
            assert status_code == 200
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_run_health_server_with_client(self):
        port = _find_free_port()

        class FakeClient:
            @property
            def identity(self):
                return "fake"

        server = await run_health_server(
            host="127.0.0.1", port=port, temporal_client=FakeClient()
        )
        try:
            status_code, body = await _http_request("127.0.0.1", port, "GET", "/ready")
            assert status_code == 200
        finally:
            await server.stop()
