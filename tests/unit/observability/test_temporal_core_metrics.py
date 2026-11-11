"""Unit tests for the bounded Temporal-core metrics reader."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import patch

import httpx
import pytest

from application_sdk.observability.temporal_core_metrics import (
    OVERSIZE_REASON,
    CoreMetricsBody,
    CoreMetricsRead,
    CoreMetricsUnavailable,
    fetch_core_metrics,
    fetch_core_metrics_blocking,
)

_URL = "http://127.0.0.1:9464/metrics"
_CAP = 1024
_EXPOSITION = b'# TYPE temporal_num_pollers gauge\ntemporal_num_pollers{poller_type="workflow_task"} 2.0\n'

Handler = Callable[[httpx.Request], httpx.Response]


class _ChunkStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body with no Content-Length, delivered as many small chunks.

    Records how many bytes were pulled so a test can prove the read stopped
    near the cap instead of draining the whole body.
    """

    def __init__(self, total: int, chunk: int = 64) -> None:
        self._total = total
        self._chunk = chunk
        self.pulled = 0

    def _chunks(self) -> Iterator[bytes]:
        while self.pulled < self._total:
            step = min(self._chunk, self._total - self.pulled)
            self.pulled += step
            yield b"x" * step

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks()

    async def __aiter__(self):
        for piece in self._chunks():
            yield piece


def _patch_transport(handler: Handler):
    """Route both httpx client classes through a MockTransport."""
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    transport = httpx.MockTransport(handler)

    def _async(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

    def _sync(*args: object, **kwargs: object) -> httpx.Client:
        return real_sync(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

    return (
        patch("httpx.AsyncClient", side_effect=_async),
        patch("httpx.Client", side_effect=_sync),
    )


async def _read(mode: str, handler: Handler) -> CoreMetricsRead:
    async_patch, sync_patch = _patch_transport(handler)
    with async_patch, sync_patch:
        if mode == "async":
            return await fetch_core_metrics(_URL, timeout_s=1.0, max_bytes=_CAP)
        return fetch_core_metrics_blocking(_URL, timeout_s=1.0, max_bytes=_CAP)


MODES = pytest.mark.parametrize("mode", ["async", "blocking"])


@MODES
async def test_returns_body_on_200(mode: str) -> None:
    read = await _read(mode, lambda _r: httpx.Response(200, content=_EXPOSITION))

    assert read == CoreMetricsBody(_EXPOSITION)


@MODES
async def test_body_exactly_at_cap_is_kept(mode: str) -> None:
    body = b"x" * _CAP
    read = await _read(mode, lambda _r: httpx.Response(200, content=body))

    assert read == CoreMetricsBody(body)


@MODES
async def test_non_200_reports_status_reason(mode: str) -> None:
    read = await _read(mode, lambda _r: httpx.Response(503, content=b"down"))

    assert read == CoreMetricsUnavailable("http_503")


@MODES
async def test_declared_oversize_is_rejected(mode: str) -> None:
    read = await _read(mode, lambda _r: httpx.Response(200, content=b"x" * (_CAP + 1)))

    assert read == CoreMetricsUnavailable(OVERSIZE_REASON)


@MODES
async def test_streamed_oversize_stops_near_the_cap(mode: str) -> None:
    """No Content-Length: the running total trips the cap mid-stream."""
    stream = _ChunkStream(total=_CAP * 100)
    read = await _read(mode, lambda _r: httpx.Response(200, stream=stream))

    assert read == CoreMetricsUnavailable(OVERSIZE_REASON)
    # Bounded: the reader stopped pulling shortly after crossing the cap
    # rather than draining all 100x of it.
    assert stream.pulled < _CAP * 3


@MODES
async def test_malformed_content_length_still_bounded(mode: str) -> None:
    stream = _ChunkStream(total=_CAP * 10)
    read = await _read(
        mode,
        lambda _r: httpx.Response(
            200, headers={"content-length": "not-a-number"}, stream=stream
        ),
    )

    assert read == CoreMetricsUnavailable(OVERSIZE_REASON)


@MODES
async def test_transport_error_propagates(mode: str) -> None:
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(httpx.ConnectError):
        await _read(mode, _refuse)


@pytest.mark.parametrize("cap", [0, -1])
@MODES
async def test_non_positive_cap_is_oversize_without_a_request(
    mode: str, cap: int
) -> None:
    """A zero/negative cap must not reach httpx as ``chunk_size`` (ValueError)."""
    requests: list[httpx.Request] = []

    def _serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=_ChunkStream(total=_CAP))

    async_patch, sync_patch = _patch_transport(_serve)
    with async_patch, sync_patch:
        if mode == "async":
            read = await fetch_core_metrics(_URL, timeout_s=1.0, max_bytes=cap)
        else:
            read = fetch_core_metrics_blocking(_URL, timeout_s=1.0, max_bytes=cap)

    assert read == CoreMetricsUnavailable(OVERSIZE_REASON)
    assert requests == []
