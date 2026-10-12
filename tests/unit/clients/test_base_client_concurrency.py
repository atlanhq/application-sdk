"""Concurrent requests through one BaseClient share one transport.

A connector typically builds a single retry transport in ``load()`` and then
fans requests out with ``asyncio.gather``. Each request must leave that shared
transport open: if a fast request's per-call client closes the connection pool
on exit, every slow request still reading from it fails with ``ReadError`` and
``execute_http_get_request`` hands back ``None``.

The unit tier blocks sockets, so ``_PoolTransport`` stands in for the
connection pool: like httpcore's pool, a request still in flight when the
transport is closed fails with ``ReadError``. The real ``httpx.AsyncClient``
still runs, including the ``__aexit__`` that closes its transport.
"""

import asyncio

import httpx
import pytest

from application_sdk.clients.base import BaseClient

_SLOW_DELAY_SECONDS = 0.2


class _LoadedClient(BaseClient):
    async def load(self, **kwargs: object) -> None:
        return None


class _PoolTransport(httpx.AsyncBaseTransport):
    """In-memory pool: ``/slow`` paths wait, and closing kills in-flight reads."""

    def __init__(self) -> None:
        self.close_calls = 0
        self._closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._closed = False  # a closed httpcore pool reopens on the next request
        if request.url.path.startswith("/slow"):
            await asyncio.sleep(_SLOW_DELAY_SECONDS)
        if self._closed:
            raise httpx.ReadError("connection pool closed mid-read", request=request)
        return httpx.Response(200, text=request.url.path, request=request)

    async def aclose(self) -> None:
        self.close_calls += 1
        self._closed = True


def _client_with_pool() -> tuple[_LoadedClient, _PoolTransport]:
    client = _LoadedClient()
    pool = _PoolTransport()
    client.http_retry_transport = pool
    return client, pool


_PATHS = [f"/slow/{i}" if i % 2 else f"/fast/{i}" for i in range(20)]


@pytest.mark.asyncio
async def test_fast_request_finishing_does_not_kill_concurrent_slow_requests() -> None:
    client, _ = _client_with_pool()

    try:
        responses = await asyncio.gather(
            *(
                client.execute_http_get_request(f"http://example.test{path}", timeout=5)
                for path in _PATHS
            )
        )
    finally:
        await client.close()

    failed = [path for path, resp in zip(_PATHS, responses) if resp is None]
    assert failed == [], f"{len(failed)} of {len(_PATHS)} requests failed: {failed}"
    assert [resp.text for resp in responses] == _PATHS


@pytest.mark.asyncio
async def test_concurrent_posts_share_transport_without_failing() -> None:
    client, _ = _client_with_pool()

    try:
        responses = await asyncio.gather(
            *(
                client.execute_http_post_request(
                    f"http://example.test{path}", json_data={}, timeout=5
                )
                for path in _PATHS
            )
        )
    finally:
        await client.close()

    assert [resp.text if resp is not None else None for resp in responses] == _PATHS


@pytest.mark.asyncio
async def test_custom_transport_set_in_load_stays_open_between_calls() -> None:
    client, pool = _client_with_pool()

    first = await client.execute_http_get_request("http://example.test/fast/a")
    second = await client.execute_http_get_request("http://example.test/fast/b")

    assert first is not None and second is not None
    assert pool.close_calls == 0, "a per-call client closed the shared transport"

    await client.close()
    assert pool.close_calls == 1, "close() must release the shared transport once"
