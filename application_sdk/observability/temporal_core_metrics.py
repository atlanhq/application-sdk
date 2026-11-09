"""Bounded reads of Temporal Rust-core's loopback Prometheus endpoint.

sdk-core serves its metrics on a local HTTP endpoint
(``ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS``). Three readers fetch it:

* the worker's poll-state diagnostic (``read_core_poller_counts``),
* the handler's ``GET /metrics`` proxy (combined mode), and
* the Pushgateway ``TemporalCoreCollector`` (worker-only mode).

All three go through the helpers here so every read is capped at
``ATLAN_TEMPORAL_CORE_METRICS_MAX_BYTES``. Without the cap, a high-cardinality
or misbehaving local exporter makes the reader allocate an unbounded body on
every scrape, push or diagnostic interval.

A read either returns :class:`CoreMetricsBody` or
:class:`CoreMetricsUnavailable` with a bounded ``reason`` (``http_<status>``
or ``oversize``). Transport failures are not caught here: they propagate as
``httpx.HTTPError`` so each caller logs them with its own level and context.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

#: ``reason`` for a body that declared, or streamed, more than the cap.
OVERSIZE_REASON = "oversize"


@dataclass(frozen=True, slots=True)
class CoreMetricsBody:
    """The exposition text, at most ``max_bytes`` long."""

    body: bytes


@dataclass(frozen=True, slots=True)
class CoreMetricsUnavailable:
    """The endpoint answered, but there is no usable body.

    ``reason`` is a bounded label value: ``http_<status>`` for a non-200
    response, or :data:`OVERSIZE_REASON` when the body exceeded the cap.
    """

    reason: str


CoreMetricsRead = CoreMetricsBody | CoreMetricsUnavailable


def _declared_oversize(headers: Mapping[str, str], max_bytes: int) -> bool:
    """True when ``Content-Length`` declares more than ``max_bytes``.

    A missing or malformed header is not a verdict: the streamed byte count
    still enforces the cap.
    """
    declared = headers.get("content-length")
    if declared is None:
        return False
    try:
        return int(declared) > max_bytes
    except ValueError:
        return False


class _BoundedBuffer:
    """Accumulates chunks until the running total crosses ``max_bytes``."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._chunks: list[bytes] = []
        self._received = 0

    def add(self, chunk: bytes) -> bool:
        """Keep ``chunk``; return False once the total exceeds the cap."""
        self._received += len(chunk)
        if self._received > self._max_bytes:
            return False
        self._chunks.append(chunk)
        return True

    def body(self) -> bytes:
        return b"".join(self._chunks)


async def fetch_core_metrics(
    url: str, *, timeout_s: float, max_bytes: int
) -> CoreMetricsRead:
    """Fetch ``url`` without retaining more than ``max_bytes`` of body.

    The response is streamed: a declared oversize ``Content-Length`` is
    rejected before reading, and otherwise the read stops as soon as the
    running total crosses the cap. ``chunk_size=max_bytes`` bounds each chunk
    handed to the loop. httpx may still buffer one raw transport chunk above
    the cap internally, so this bounds what is retained, not every transient
    allocation.

    Raises:
        httpx.HTTPError: the request failed (connect, timeout, protocol).
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return CoreMetricsUnavailable(f"http_{response.status_code}")
            if _declared_oversize(response.headers, max_bytes):
                return CoreMetricsUnavailable(OVERSIZE_REASON)
            buffer = _BoundedBuffer(max_bytes)
            async for chunk in response.aiter_bytes(chunk_size=max_bytes):
                if not buffer.add(chunk):
                    return CoreMetricsUnavailable(OVERSIZE_REASON)
            return CoreMetricsBody(buffer.body())


def fetch_core_metrics_blocking(
    url: str, *, timeout_s: float, max_bytes: int
) -> CoreMetricsRead:
    """Blocking twin of :func:`fetch_core_metrics`, for ``Collector.collect``.

    ``prometheus_client`` calls collectors synchronously; the Pushgateway
    client already runs that call off the event loop.

    Raises:
        httpx.HTTPError: the request failed (connect, timeout, protocol).
    """
    with httpx.Client(timeout=timeout_s) as client:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                return CoreMetricsUnavailable(f"http_{response.status_code}")
            if _declared_oversize(response.headers, max_bytes):
                return CoreMetricsUnavailable(OVERSIZE_REASON)
            buffer = _BoundedBuffer(max_bytes)
            for chunk in response.iter_bytes(chunk_size=max_bytes):
                if not buffer.add(chunk):
                    return CoreMetricsUnavailable(OVERSIZE_REASON)
            return CoreMetricsBody(buffer.body())
