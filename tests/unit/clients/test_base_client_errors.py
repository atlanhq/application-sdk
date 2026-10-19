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
from loguru import logger as _loguru_logger

from application_sdk.clients.base import BaseClient
from application_sdk.clients.http_errors import HttpClientSourceUnavailableError
from application_sdk.errors import FailureCategory
from application_sdk.errors.leaves import SourceUnavailableError
from application_sdk.observability.logger_adaptor import (
    _extract_exception_attributes,
    _redact_exception_records,
)

_URL = "http://user:pw@source.example.test:8080/api/keys/FAKEPATH/items?token=secret#frag"
_ENDPOINT = "http://source.example.test:8080"


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
    assert isinstance(error, HttpClientSourceUnavailableError)
    assert error.code == "SOURCE_UNAVAILABLE_HTTP_CLIENT"
    assert backend.connect_attempts == 4, "one attempt plus three retries"
    assert error.category is FailureCategory.SOURCE_UNAVAILABLE
    assert error.effective_retryable is True
    assert isinstance(error.cause, httpx.ConnectError)
    assert error.__cause__ is error.cause
    assert error.network_error == "ConnectError"
    assert error.endpoint == _ENDPOINT
    assert error.message == f"HTTP {method} {_ENDPOINT} failed: ConnectError"
    for leaked in ("secret", "pw", "FAKEPATH"):
        assert leaked not in error.message and leaked not in str(error.endpoint)


async def test_connect_failure_without_raise_on_error_returns_none_and_warns() -> None:
    client, _ = _client_with_backend(failures=100)
    try:
        with pytest.warns(DeprecationWarning, match="raise_on_error=True") as record:
            result = await client.execute_http_get_request(_URL)
    finally:
        await client.close()

    assert result is None
    assert record[0].filename == __file__, "warning must point at the caller"


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


def _raising(exc_factory: Any) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_factory(request)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "exc_type", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError]
)
async def test_source_transport_failures_raise_source_unavailable(
    exc_type: type[httpx.RequestError],
) -> None:
    client = _LoadedClient()
    client.http_retry_transport = _raising(lambda r: exc_type("boom", request=r))
    try:
        with pytest.raises(SourceUnavailableError) as raised:
            await client.execute_http_get_request(_URL, raise_on_error=True)
    finally:
        await client.close()

    assert isinstance(raised.value.cause, exc_type)


@pytest.mark.parametrize(
    "exc_type",
    [httpx.PoolTimeout, httpx.UnsupportedProtocol, httpx.TooManyRedirects],
    ids=["own-pool-exhausted", "bad-scheme", "redirect-loop"],
)
async def test_non_source_httpx_errors_propagate_unchanged(
    exc_type: type[httpx.RequestError],
) -> None:
    """Not the source's fault, so not a retryable SourceUnavailableError."""
    client = _LoadedClient()
    client.http_retry_transport = _raising(lambda r: exc_type("boom", request=r))
    try:
        with pytest.raises(exc_type):
            await client.execute_http_get_request(_URL, raise_on_error=True)
    finally:
        await client.close()


async def test_unsupported_scheme_on_default_transport_propagates_unchanged() -> None:
    client = _LoadedClient()
    try:
        with pytest.raises(httpx.UnsupportedProtocol):
            await client.execute_http_get_request(
                "ftp://source.example.test/x", raise_on_error=True
            )
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("exc_factory", "exc_name"),
    [
        (
            lambda r: httpx.ConnectError(f"cannot reach {r.url}", request=r),
            "ConnectError",
        ),
        (lambda r: RuntimeError(f"cannot reach {r.url}"), "RuntimeError"),
        (lambda r: RuntimeError(f"cannot reach {r.url}#k=fr4g"), "RuntimeError"),
        (lambda r: RuntimeError("cannot reach: token=s3cret"), "RuntimeError"),
    ],
    ids=["httpx-quotes-url", "custom-quotes-url", "fragment", "bare-param"],
)
async def test_failure_log_keeps_the_traceback_and_leaves_redacted(
    exc_factory: Any, exc_name: str, loguru_capture: list[dict[str, Any]]
) -> None:
    """The failure is logged with its full traceback, redacted on the way out.

    The shapes are the ones a transport's message can carry: the sent URL with
    ``params`` merged into the query, a fragment, a bare ``token=``. Both exits
    are checked: the OTel exception attributes and the rendered console line.
    """
    lines: list[str] = []
    sink_id = _loguru_logger.add(
        _redact_exception_records(lines.append), format="{message}\n{exception}"
    )
    client = _LoadedClient()
    client.http_retry_transport = _raising(exc_factory)
    try:
        result = await client.execute_http_get_request(
            _URL, params={"apikey": "p4ram"}, raise_on_error=False
        )
    finally:
        _loguru_logger.remove(sink_id)
        await client.close()

    assert result is None
    (record,) = [r for r in loguru_capture if "Request failed" in str(r["message"])]
    assert record["message"] == f"Request failed for GET {_ENDPOINT}"
    assert record["exception"] is not None, "the traceback must be logged"
    attrs = _extract_exception_attributes(record["exception"])
    (line,) = [entry for entry in lines if "Request failed" in entry]
    for exported in (attrs["exception.message"], attrs["exception.stacktrace"], line):
        assert "cannot reach" in exported, "the root-cause message must survive"
        for secret in ("secret", "s3cret", "p4ram", "fr4g", "pw@"):
            assert secret not in exported
    assert attrs["exception.type"].endswith(exc_name)
    for rendered in (attrs["exception.stacktrace"], line):
        assert "in handler" in rendered, "the frames must survive"
