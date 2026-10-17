"""The sidecar base URL must be an IP literal, so reaching it needs no name lookup."""

from __future__ import annotations

import asyncio
import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx
import pytest

from application_sdk.infrastructure._dapr.http import _dapr_base_url


def test_base_url_is_ipv4_loopback_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAPR_HTTP_PORT", "4567")

    url = _dapr_base_url()

    assert url == "http://127.0.0.1:4567"
    ipaddress.ip_address(urlsplit(url).hostname or "")  # raises if a hostname


async def test_request_succeeds_while_default_executor_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("DAPR_HTTP_PORT", str(port))

    release = threading.Event()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    blocker = loop.run_in_executor(None, release.wait)  # the pool's only worker
    try:
        async with httpx.AsyncClient(
            base_url=_dapr_base_url(), timeout=httpx.Timeout(2.0)
        ) as client:
            resp = await client.post("/v1.0/healthz")
        assert resp.status_code == 204
    finally:
        release.set()
        await blocker
        server.close()
        await server.wait_closed()
        executor.shutdown(wait=True)
