"""Concurrent requests through one BaseClient share one transport.

A connector typically builds a single retry transport in ``load()`` and then
fans requests out with ``asyncio.gather``. Each request must leave that shared
transport open: if a fast request's per-call client closes the connection pool
on exit, every slow request still reading from it fails with ``ReadError`` and
``execute_http_get_request`` hands back ``None``.

These tests run against a real local HTTP server because the failure lives in
the connection pool — a mocked transport has no sockets to close.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from application_sdk.clients.base import BaseClient

_FAST_DELAY_SECONDS = 0.0
_SLOW_DELAY_SECONDS = 0.5


class _LoadedClient(BaseClient):
    async def load(self, **kwargs: object) -> None:
        return None


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal HTTP/1.1 server: ``/slow/<n>`` waits before answering."""
    try:
        while True:
            request_line = await reader.readline()
            if not request_line:
                return
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            path = request_line.split()[1].decode()
            delay = _SLOW_DELAY_SECONDS if path.startswith("/slow") else 0.0
            await asyncio.sleep(delay or _FAST_DELAY_SECONDS)
            body = path.encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        return
    finally:
        writer.close()


@pytest.fixture
async def local_server() -> AsyncIterator[str]:
    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fast_request_finishing_does_not_kill_concurrent_slow_requests(
    local_server: str,
) -> None:
    client = _LoadedClient()
    paths = [f"/slow/{i}" if i % 2 else f"/fast/{i}" for i in range(20)]

    try:
        responses = await asyncio.gather(
            *(
                client.execute_http_get_request(f"{local_server}{path}", timeout=5)
                for path in paths
            )
        )
    finally:
        await client.close()

    failed = [path for path, resp in zip(paths, responses) if resp is None]
    assert failed == [], f"{len(failed)} of {len(paths)} requests failed: {failed}"
    assert [resp.text for resp in responses] == paths


@pytest.mark.asyncio
async def test_concurrent_posts_share_transport_without_failing(
    local_server: str,
) -> None:
    client = _LoadedClient()
    paths = [f"/slow/{i}" if i % 2 else f"/fast/{i}" for i in range(20)]

    try:
        responses = await asyncio.gather(
            *(
                client.execute_http_post_request(
                    f"{local_server}{path}", json_data={}, timeout=5
                )
                for path in paths
            )
        )
    finally:
        await client.close()

    assert [resp.text if resp is not None else None for resp in responses] == paths


@pytest.mark.asyncio
async def test_custom_transport_set_in_load_stays_open_between_calls(
    local_server: str,
) -> None:
    client = _LoadedClient()
    closed: list[str] = []

    class _RecordingTransport(httpx.AsyncHTTPTransport):
        async def __aexit__(self, *args: object) -> None:
            closed.append("__aexit__")
            await super().__aexit__(*args)  # type: ignore[arg-type]

        async def aclose(self) -> None:
            closed.append("aclose")
            await super().aclose()

    client.http_retry_transport = _RecordingTransport()

    first = await client.execute_http_get_request(f"{local_server}/fast/a")
    second = await client.execute_http_get_request(f"{local_server}/fast/b")

    assert first is not None and second is not None
    assert closed == [], "a per-call client closed the shared transport"

    await client.close()
    assert closed == ["aclose"], "close() must release the shared transport once"
