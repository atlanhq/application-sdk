"""BaseClient HTTP helpers: connect retries, typed failures, the transport hook.

These run through the real default ``httpx.AsyncHTTPTransport`` and its
httpcore pool. The unit tier blocks sockets, so the pool's network backend is
swapped for an httpcore mock backend that refuses the first ``failures``
connection attempts, then serves a canned HTTP/1.1 200. The mock backend's
``sleep`` is a no-op, so httpcore's retry backoff costs no wall-clock time.
"""

import warnings
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from application_sdk.clients.base import BaseClient
from application_sdk.errors import FailureCategory
from application_sdk.errors.leaves import SourceUnavailableError

_URL = "http://user:pw@source.example.test:8080/api/items?token=secret#frag"
_ENDPOINT = "http://source.example.test:8080/api/items"


class _LoadedClient(BaseClient):
    async def load(self, **kwargs: Any) -> None:
        return None


class _RefusingBackend(httpcore.AsyncMockBackend):
    """Refuses the first ``failures`` TCP connects, then serves one 200."""

    def __init__(self, failures: int) -> None:
        super().__init__(
            buffer=[b"HTTP/1.1 200 OK\r\n", b"Content-Length: 2\r\n", b"\r\n", b"ok"]
        )
        self.failures = failures
        self.connect_attempts = 0

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connect_attempts += 1
        if self.connect_attempts <= self.failures:
            raise httpcore.ConnectError("[Errno 111] Connection refused")
        return await super().connect_tcp(
            host, port, timeout, local_address, socket_options
        )


def _client_with_backend(failures: int) -> tuple[_LoadedClient, _RefusingBackend]:
    client = _LoadedClient()
    backend = _RefusingBackend(failures)
    transport = client.http_retry_transport
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    transport._pool._network_backend = backend
    return client, backend


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_connect_failure_is_retried_then_succeeds(method: str) -> None:
    client, backend = _client_with_backend(failures=3)
    try:
        if method == "GET":
            response = await client.execute_http_get_request(_URL, raise_on_error=True)
        else:
            response = await client.execute_http_post_request(
                _URL, json_data={"a": 1}, raise_on_error=True
            )
    finally:
        await client.close()

    assert response.status_code == 200
    assert response.text == "ok"
    assert backend.connect_attempts == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_connect_failure_after_retries_raises_source_unavailable(
    method: str,
) -> None:
    client, backend = _client_with_backend(failures=100)
    try:
        with pytest.raises(SourceUnavailableError) as raised:
            if method == "GET":
                await client.execute_http_get_request(_URL, raise_on_error=True)
            else:
                await client.execute_http_post_request(
                    _URL, json_data={"a": 1}, raise_on_error=True
                )
    finally:
        await client.close()

    error = raised.value
    assert backend.connect_attempts == 4, "one attempt plus three retries"
    assert error.category is FailureCategory.SOURCE_UNAVAILABLE
    assert error.effective_retryable is True
    assert isinstance(error.cause, httpx.ConnectError)
    assert error.__cause__ is error.cause
    assert error.network_error == "ConnectError"
    assert error.endpoint == _ENDPOINT
    assert error.message == f"HTTP {method} {_ENDPOINT} failed: ConnectError"
    assert "secret" not in error.message and "pw" not in error.message


@pytest.mark.asyncio
async def test_connect_failure_without_raise_on_error_returns_none_and_warns() -> None:
    client, _ = _client_with_backend(failures=100)
    try:
        with pytest.warns(DeprecationWarning, match="raise_on_error=True") as record:
            result = await client.execute_http_get_request(_URL)
    finally:
        await client.close()

    assert result is None
    assert record[0].filename == __file__, "warning must point at the caller"


@pytest.mark.asyncio
async def test_explicit_false_returns_none_without_warning() -> None:
    client, _ = _client_with_backend(failures=100)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await client.execute_http_post_request(
                _URL, json_data={}, raise_on_error=False
            )
    finally:
        await client.close()

    assert result is None


@pytest.mark.asyncio
async def test_raise_on_error_returns_error_status_responses() -> None:
    """A 5xx is a response, not a missing one: it is returned, never raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    client = _LoadedClient()
    client.http_retry_transport = httpx.MockTransport(handler)
    try:
        response = await client.execute_http_get_request(_URL, raise_on_error=True)
    finally:
        await client.close()

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_raise_on_error_propagates_non_network_errors_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in a custom transport")

    client = _LoadedClient()
    client.http_retry_transport = httpx.MockTransport(handler)
    try:
        with pytest.raises(RuntimeError, match="bug in a custom transport"):
            await client.execute_http_get_request(_URL, raise_on_error=True)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_retry_transport_set_in_load_is_used() -> None:
    """The documented hook: assigning ``http_retry_transport`` in ``load()``."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, text="custom", request=request)

    class _CustomTransportClient(BaseClient):
        async def load(self, **kwargs: Any) -> None:
            self.http_retry_transport = httpx.MockTransport(handler)

    async with _CustomTransportClient() as client:
        await client.load()
        got = await client.execute_http_get_request(_URL, raise_on_error=True)
        posted = await client.execute_http_post_request(
            _URL, json_data={}, raise_on_error=True
        )

    assert got.text == "custom" and posted.text == "custom"
    assert seen == ["GET", "POST"]
