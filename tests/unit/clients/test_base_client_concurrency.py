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
import gc
import warnings
from collections.abc import Awaitable, Callable

import httpx
import pytest

from application_sdk.clients import base as base_module
from application_sdk.clients.base import BaseClient

_SLOW_DELAY_SECONDS = 0.2


class _LoadedClient(BaseClient):
    async def load(self, **kwargs: object) -> None:
        return None


class _PoolTransport(httpx.AsyncBaseTransport):
    """In-memory pool: ``/slow`` paths wait, and closing kills in-flight reads."""

    def __init__(self) -> None:
        self.enter_calls = 0
        self.close_calls = 0
        self._closed = False

    async def __aenter__(self) -> "_PoolTransport":
        self.enter_calls += 1
        self._closed = False
        return self

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.enter_calls == 0:
            raise RuntimeError("transport used before __aenter__")
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


@pytest.mark.asyncio
async def test_async_with_closes_the_shared_transport_on_exit() -> None:
    client, pool = _client_with_pool()

    async with client as entered:
        assert entered is client
        assert await client.execute_http_get_request("http://example.test/fast/a")
        assert pool.close_calls == 0

    assert pool.close_calls == 1


@pytest.mark.asyncio
async def test_async_with_closes_the_shared_transport_when_the_block_raises() -> None:
    client, pool = _client_with_pool()

    with pytest.raises(RuntimeError, match="boom"):
        async with client:
            raise RuntimeError("boom")

    assert pool.close_calls == 1


class _CloseWithoutSuper(_LoadedClient):
    async def close(self, *args: object, **kwargs: object) -> None:
        return None


async def _resource_warnings(scenario: Callable[[], Awaitable[None]]) -> list[str]:
    """Run a scenario that drops its client, then collect it."""
    gc.collect()  # drop other tests' garbage so only this scenario's client counts
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await scenario()
        gc.collect()
    return [str(w.message) for w in caught if w.category is ResourceWarning]


@pytest.mark.asyncio
async def test_unclosed_client_warns_when_collected() -> None:
    async def unclosed() -> None:
        client, _ = _client_with_pool()
        await client.execute_http_get_request("http://example.test/fast/a")

    messages = await _resource_warnings(unclosed)

    assert len(messages) == 1 and "_LoadedClient was not closed" in messages[0]


@pytest.mark.asyncio
async def test_close_override_without_super_warns_when_collected() -> None:
    async def override_skips_super() -> None:
        client = _CloseWithoutSuper()
        client.http_retry_transport = _PoolTransport()
        async with client:
            await client.execute_http_get_request("http://example.test/fast/a")

    assert len(await _resource_warnings(override_skips_super)) == 1


@pytest.mark.asyncio
async def test_closed_or_unused_client_does_not_warn() -> None:
    async def closed() -> None:
        client, _ = _client_with_pool()
        async with client:
            await client.execute_http_get_request("http://example.test/fast/a")

    async def unused() -> None:
        _client_with_pool()

    assert await _resource_warnings(closed) == []
    assert await _resource_warnings(unused) == []


@pytest.mark.asyncio
async def test_shared_transport_is_entered_once_and_exited_by_close() -> None:
    client, pool = _client_with_pool()

    await asyncio.gather(
        *(client.execute_http_get_request(f"http://example.test{p}") for p in _PATHS)
    )
    assert (
        pool.enter_calls == 1
    ), "concurrent first requests entered the transport twice"
    assert pool.close_calls == 0

    await client.close()
    assert pool.close_calls == 1

    assert await client.execute_http_get_request("http://example.test/fast/again")
    assert pool.enter_calls == 2, "a request after close() must re-enter the transport"
    await client.close()


@pytest.mark.asyncio
async def test_finalizer_tolerates_cleared_module_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client_with_pool()
    await client.execute_http_get_request("http://example.test/fast/a")
    monkeypatch.setattr(base_module, "warnings", None)  # as at interpreter teardown

    client.__del__()  # must not raise

    monkeypatch.undo()
    await client.close()
