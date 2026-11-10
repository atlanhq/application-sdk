import asyncio
import base64
import logging
import threading
import weakref
from collections import deque
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from application_sdk.constants import (
    SEGMENT_API_URL,
    SEGMENT_BATCH_SIZE,
    SEGMENT_BATCH_TIMEOUT_SECONDS,
    SEGMENT_DEFAULT_USER_ID,
    SEGMENT_WRITE_KEY,
)
from application_sdk.observability.background_loop import (
    BackgroundTask,
    observability_loop,
    register_after_fork,
)
from application_sdk.observability.models import MetricRecord
from application_sdk.observability.utils import in_temporal_workflow
from application_sdk.version import __version__

# Default cap on queued events. Without one, a Segment outage grows the queue
# for as long as metrics are emitted; at the cap new events are dropped and
# counted (``SegmentClient.dropped_count``).
_DEFAULT_MAX_QUEUE_SIZE = 10_000


class SegmentTrackEvent(BaseModel):
    """Pydantic model for a single Segment track event.

    Attributes:
        userId: The user ID for the event
        event: The event name
        properties: Event properties/metadata
        timestamp: ISO 8601 timestamp of the event
        type: Event type (always "track" for track events)
    """

    userId: str = Field(..., description="User ID for the event")
    event: str = Field(..., description="Event name")
    properties: dict[str, Any] = Field(
        default_factory=dict, description="Event properties"
    )
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    type: str = Field(default="track", description="Event type")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "userId": "atlan.automation",
                "event": "metric_recorded",
                "properties": {"value": 100, "metric_type": "counter"},
                "timestamp": "2024-01-01T00:00:00",
                "type": "track",
            }
        }
    )


class SegmentBatchPayload(BaseModel):
    """Pydantic model for Segment batch API payload.

    Attributes:
        batch: List of track events to send in batch
    """

    batch: list[SegmentTrackEvent] = Field(
        ..., description="List of events to send in batch"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch": [
                    {
                        "userId": "atlan.automation",
                        "event": "metric_recorded",
                        "properties": {"value": 100},
                        "timestamp": "2024-01-01T00:00:00",
                        "type": "track",
                    }
                ]
            }
        }
    )


class SegmentClient:
    """Async Segment client with queue-based event processing.

    This client uses an asyncio queue to batch and send metrics to Segment API
    asynchronously. The queue worker runs on the shared observability loop
    (``background_loop``), not on a thread of its own.

    The client is automatically enabled when a valid write key is provided.
    No separate boolean flag is needed - the presence of the key determines
    whether events are sent.

    Attributes:
        enabled (bool): Whether Segment client is enabled (has valid write key)
        _queue (asyncio.Queue): Queue for pending metric events
        _client (httpx.AsyncClient): Async HTTP client for API calls
        _worker (BackgroundTask | None): Handle to the queue worker on the
            shared observability loop
    """

    def __init__(
        self,
        write_key: str = "",
        api_url: str = "",
        default_user_id: str = "",
        batch_size: int = 0,
        batch_timeout_seconds: float = 0.0,
        max_queue_size: int = 0,
    ):
        """Initialize Segment client.

        The client is automatically enabled if a valid write key is provided.
        No separate enabled flag is needed.

        Args:
            write_key: Segment write key for authentication (defaults to SEGMENT_WRITE_KEY)
            api_url: Segment API URL (defaults to SEGMENT_API_URL)
            default_user_id: Default user ID for events (defaults to SEGMENT_DEFAULT_USER_ID)
            batch_size: Maximum number of events per batch (defaults to SEGMENT_BATCH_SIZE)
            batch_timeout_seconds: Max seconds to wait before sending batch (defaults to SEGMENT_BATCH_TIMEOUT_SECONDS)
            max_queue_size: Maximum number of events waiting to be handed to the
                worker loop, and separately of events queued there; further
                events are dropped and counted in ``dropped_count`` (defaults
                to 10 000). Must not be negative: ``asyncio.Queue`` treats a
                non-positive size as unbounded.

        Raises:
            ValueError: If ``max_queue_size`` is negative.
        """
        if max_queue_size < 0:
            raise ValueError(f"max_queue_size must be >= 0, got {max_queue_size}")
        self._write_key = write_key or SEGMENT_WRITE_KEY
        self._api_url = api_url or SEGMENT_API_URL
        self._default_user_id = default_user_id or SEGMENT_DEFAULT_USER_ID
        self._batch_size = batch_size or SEGMENT_BATCH_SIZE
        self._batch_timeout_seconds = (
            batch_timeout_seconds or SEGMENT_BATCH_TIMEOUT_SECONDS
        )
        self._max_queue_size = max_queue_size or _DEFAULT_MAX_QUEUE_SIZE
        self._queue: asyncio.Queue[MetricRecord] | None = None
        self._client: httpx.AsyncClient | None = None
        # The queue worker, running on the shared observability loop
        # (``background_loop``), and that loop.
        self._worker: BackgroundTask | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Serialises worker start: concurrent ``send_metric`` calls that all
        # see no live worker would otherwise each start one.
        self._start_lock = threading.Lock()
        # Set by ``close()``. A closed client drops events instead of starting a
        # worker nothing will ever stop.
        self._closed = False
        # Hand-off from ``send_metric`` (any thread) to the worker loop. Capped
        # at ``_max_queue_size`` at admission, so a worker loop that falls
        # behind cannot accumulate one scheduled callback per event: at most
        # one ``_drain_ingress`` callback is pending (``_drain_scheduled``).
        self._ingress: deque[MetricRecord] = deque()
        self._drain_scheduled = False
        # Guards ``_ingress``, ``_drain_scheduled``, ``_dropped_count`` and
        # ``_overflowing``.
        self._ingress_lock = threading.Lock()
        # Events dropped because the hand-off or the queue was full.
        self._dropped_count = 0
        # True while events are being dropped; the drop warning fires once per
        # episode and re-arms when an event reaches the queue again.
        self._overflowing = False

        # Enable client if write key is present
        self.enabled = bool(self._write_key)

        if not self.enabled:
            logging.warning(
                "Segment write key not configured - Segment metrics will be disabled"
            )
            return

        _live_clients.add(self)
        # Start the queue worker on the shared observability loop
        self._start_worker()

        logging.info(
            "Segment metrics client initialized (batch_size=%d, batch_timeout=%ss)",
            self._batch_size,
            self._batch_timeout_seconds,
        )

    def _reset_after_fork(self) -> None:
        """In a forked child, drop the worker that ran on the parent's loop.

        The next ``send_metric`` starts a worker on the child's loop. Locks
        are replaced because a parent thread may have held one at the fork;
        events handed off to the parent's loop are dropped with it.
        """
        self._start_lock = threading.Lock()
        self._ingress_lock = threading.Lock()
        self._ingress.clear()
        self._drain_scheduled = False
        self._worker = None
        self._loop = None
        self._queue = None
        self._client = None

    @property
    def dropped_count(self) -> int:
        """Events dropped because the hand-off or the queue was at ``max_queue_size``."""
        return self._dropped_count

    def _start_worker(self) -> None:
        """Start the queue worker on the shared observability loop.

        No-op if the worker is running or the client is closed. Serialised by
        ``_start_lock`` so concurrent callers start at most one worker.

        The queue and HTTP client are built here, on the caller's thread:
        neither binds to a loop until first awaited, so ``send_metric`` can
        hand off to the queue as soon as this returns, with no wait for the
        worker to come up.
        """
        with self._start_lock:
            if self._closed:
                return
            if self._worker is not None and not self._worker.done():
                return
            if observability_loop.closed:
                # The observability loop has shut down: nothing can send.
                return
            from application_sdk.constants import (  # noqa: PLC0415 — circular: observability is imported transitively by many modules; lifting risks circles
                _HTTP_POOL_TIMEOUT_SECONDS,
            )

            queue: asyncio.Queue[MetricRecord] = asyncio.Queue(
                maxsize=self._max_queue_size
            )
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, pool=_HTTP_POOL_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30.0,
                ),
            )
            # Stopped in a later stage than the periodic flushes: the metrics
            # final flush drains this queue through this HTTP client, which
            # the worker closes as it exits.
            worker = observability_loop.start(
                self._run_worker(queue, client),
                name="segment-worker",
                stop_order=1,
            )
            if worker is None:
                # The observability loop has shut down: nothing can send.
                self._loop = None
                self._queue = None
                self._client = None
                self._worker = None
                return
            self._queue = queue
            self._client = client
            self._loop = worker.loop
            self._worker = worker

    async def _run_worker(
        self, queue: "asyncio.Queue[MetricRecord]", client: httpx.AsyncClient
    ) -> None:
        """Process the queue until cancelled, then close the HTTP client.

        The client is closed here, after ``_process_queue``'s
        ``CancelledError`` handler has sent the final batch, so the connection
        pool is never torn down mid-request.
        """
        try:
            await self._process_queue(queue, client)
        finally:
            try:
                await client.aclose()
            except Exception:
                logging.warning("Error closing httpx client", exc_info=True)

    async def flush(self) -> None:
        """Flush queued events to Segment.

        Drains the asyncio queue and sends any queued events immediately.
        Events already dequeued by the worker into its in-flight batch are
        sent when close() cancels the worker task — call close() after flush()
        for a complete drain.
        Bridges to the worker's dedicated event loop via asyncio.wrap_future.
        Times out after 10 s to keep shutdown bounded. At process exit it
        runs under the post-drain exit deadline, which is derived with the
        rest of the shutdown bounds from the pod's grace period (see
        ``application_sdk.constants.ShutdownBudget``).

        No-op when any of these hold:

        - the client is disabled (no write key) or the worker loop is not running;
        - the caller is inside a Temporal workflow. ``asyncio.wrap_future``
          resolves the awaited future from a done-callback that first calls
          ``is_closed()`` on the destination loop; Temporal's deterministic
          workflow loop raises ``NotImplementedError`` there, so the callback
          dies before resolving and the await burns the full 10 s timeout
          (SYSAPPS-328). Queued events are not lost — the worker's own batch
          timer (``SEGMENT_BATCH_TIMEOUT_SECONDS``) sends them, and ``close()``
          drains the remainder at process exit.
        """
        if not self.enabled or not self._loop or not self._queue:
            return
        if not self._loop.is_running():
            return

        if in_temporal_workflow():
            return

        if asyncio.get_running_loop() is self._loop:
            # Already on the worker's loop (the metrics adapter's periodic
            # flush runs there): no bridge needed.
            try:
                await asyncio.wait_for(self._flush_queue(), timeout=10.0)
            except TimeoutError:
                logging.warning("Segment queue flush timed out", exc_info=True)
            except Exception:
                logging.warning("Error flushing Segment queue", exc_info=True)
            return

        future = asyncio.run_coroutine_threadsafe(self._flush_queue(), self._loop)
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=10.0)
        except TimeoutError:
            future.cancel()
            logging.warning("Segment queue flush timed out", exc_info=True)
        except Exception:
            logging.warning("Error flushing Segment queue", exc_info=True)

    def send_metric(self, metric_record: "MetricRecord") -> None:
        """Send a metric to Segment API via queue (synchronous interface).

        Args:
            metric_record (MetricRecord): Metric record to send

        This method is non-blocking - it adds the metric to the queue and returns.
        Only metrics that pass the allow list check will be sent.
        """
        if not self.enabled or self._closed:
            return

        # Check if metric should be sent (allow list filtering)
        if not self._should_send_metric(metric_record):
            return

        # Ensure the worker is running
        if self._worker is None or self._worker.done():
            self._start_worker()
            if self._closed:
                return

        if not self._loop or not self._queue:
            return

        with self._ingress_lock:
            if len(self._ingress) >= self._max_queue_size:
                warn = self._count_drops_locked(1)
                schedule = False
            else:
                warn = False
                self._ingress.append(metric_record)
                schedule = not self._drain_scheduled
                self._drain_scheduled = True
        if warn:
            self._warn_dropping()
        if not schedule:
            return

        try:
            # One drain callback per batch of hand-offs, not one per event, and
            # ``put_nowait`` there rather than ``await put``: either alternative
            # would hold events outside the caps for as long as the loop lags.
            self._loop.call_soon_threadsafe(self._drain_ingress)
        except Exception:
            with self._ingress_lock:
                self._drain_scheduled = False
            logging.warning("Failed to queue metric for Segment", exc_info=True)

    def _count_drops_locked(self, n: int) -> bool:
        """Count *n* dropped events; True if this starts an overflow episode.

        Caller holds ``_ingress_lock``.
        """
        self._dropped_count += n
        if self._overflowing:
            return False
        self._overflowing = True
        return True

    def _warn_dropping(self) -> None:
        logging.warning(
            "Segment queue full (%d events); dropping new events (dropped so far: %d)",
            self._max_queue_size,
            self._dropped_count,
        )

    def _drain_ingress(self) -> None:
        """Move handed-off events into the queue, dropping any that do not fit.

        Runs on the worker loop. Checked with ``full()`` rather than catching
        ``QueueFull``: only this loop touches the queue, so the check cannot go
        stale before ``put_nowait``, and a full queue is an expected state.
        """
        with self._ingress_lock:
            records = list(self._ingress)
            self._ingress.clear()
            self._drain_scheduled = False
        if self._queue is None:
            return
        dropped = 0
        accepted = False
        for record in records:
            if self._queue.full():
                dropped += 1
            else:
                self._queue.put_nowait(record)
                accepted = True
        warn = False
        with self._ingress_lock:
            if accepted:
                self._overflowing = False
            if dropped:
                warn = self._count_drops_locked(dropped)
        if warn:
            self._warn_dropping()

    async def _process_queue(
        self, queue: "asyncio.Queue[MetricRecord]", client: httpx.AsyncClient
    ) -> None:
        """Background worker that processes metrics from the queue.

        Continuously processes metrics from the queue and sends them to Segment API in batches.
        """

        batch: list[MetricRecord] = []
        last_send_time = asyncio.get_running_loop().time()

        while True:
            try:
                # Calculate remaining time until batch timeout
                current_time = asyncio.get_running_loop().time()
                time_since_last_send = current_time - last_send_time
                timeout = max(0.1, self._batch_timeout_seconds - time_since_last_send)

                # Get metric from queue (with timeout)
                try:
                    metric_record = await asyncio.wait_for(queue.get(), timeout=timeout)
                    batch.append(metric_record)
                    queue.task_done()
                except TimeoutError:  # conformance: ignore[E002,E014] queue.get timeout = flush the partial batch now
                    pass

                current_time = asyncio.get_running_loop().time()

                # Send batch if we've reached batch size or timeout
                should_send = len(batch) >= self._batch_size or (
                    len(batch) > 0
                    and (current_time - last_send_time) >= self._batch_timeout_seconds
                )

                if should_send and batch:
                    await self._send_batch_to_segment(batch, client)
                    batch = []
                    last_send_time = asyncio.get_running_loop().time()

            except asyncio.CancelledError:
                # Worker task cancelled - send remaining batch before exit
                if batch:
                    try:
                        await self._send_batch_to_segment(batch, client)
                    except Exception:
                        logging.warning("Error sending final batch", exc_info=True)
                break
            except Exception:
                logging.warning("Error processing Segment metric queue", exc_info=True)

    async def _flush_queue(self) -> None:
        """Drain the queue and send pending events in ``_batch_size`` chunks.

        One request per chunk, not one for the whole backlog: after an outage
        the backlog can be far larger than Segment accepts in a single batch.

        Runs inside the worker's event loop. Called by flush() via
        run_coroutine_threadsafe to bridge from the main event loop.
        """
        if not self._queue or not self._client:
            return
        batch: list[MetricRecord] = []
        while not self._queue.empty():
            try:
                metric_record = self._queue.get_nowait()
                batch.append(metric_record)
                self._queue.task_done()
            except asyncio.QueueEmpty:  # conformance: ignore[E014] QueueEmpty on get_nowait terminates drain loop; not an error
                break
            if len(batch) >= self._batch_size:
                await self._send_batch_to_segment(batch)
                batch = []
        if batch:
            await self._send_batch_to_segment(batch)

    def _build_track_event(self, metric_record: "MetricRecord") -> SegmentTrackEvent:
        """Build a Segment track event from a metric record.

        Args:
            metric_record: Metric record to convert

        Returns:
            SegmentTrackEvent model
        """
        # Build event properties
        event_properties = {
            "value": metric_record.value,
            "metric_type": metric_record.type.value,
            "sdk_version": __version__,
            **metric_record.labels,
        }

        # Add optional fields if present
        if metric_record.description:
            event_properties["description"] = metric_record.description
        if metric_record.unit:
            event_properties["unit"] = metric_record.unit

        # Per-tenant identity. Without this, every event across every customer
        # tenant shares one Mixpanel distinct_id and per-customer analytics
        # collapse to a single synthetic user. The default user id is kept as
        # a namespace prefix so events across services remain easy to group;
        # the tenant identifier is appended when present in labels. Candidates
        # are checked in increasing-genericity order.
        tenant_suffix = (
            metric_record.labels.get("tenant_name")
            or metric_record.labels.get("atlan_base_url")
            or metric_record.labels.get("tenant_id")
        )
        user_id = (
            f"{self._default_user_id}:{tenant_suffix}"
            if tenant_suffix
            else self._default_user_id
        )

        # Create and return Pydantic model
        return SegmentTrackEvent(
            userId=user_id,
            event=metric_record.name,
            properties=event_properties,
            timestamp=datetime.fromtimestamp(metric_record.timestamp).isoformat(),
        )

    async def _send_batch_to_segment(
        self,
        metric_records: list["MetricRecord"],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Send a batch of metrics to Segment API.

        Args:
            metric_records: List of metric records to send
            client: HTTP client to send with; defaults to the current worker's.
        """
        client = client or self._client
        if not client or not metric_records:
            return

        try:
            # Build batch of track events
            events = [self._build_track_event(record) for record in metric_records]

            # Create batch payload
            batch_payload = SegmentBatchPayload(batch=events)

            # Create Basic Auth header
            segment_write_key_encoded = base64.b64encode(
                (self._write_key + ":").encode("ascii")
            ).decode()

            headers = {
                "content-type": "application/json",
                "Authorization": f"Basic {segment_write_key_encoded}",
            }

            # Send HTTP request with validated batch payload
            response = await client.post(
                self._api_url,
                json=batch_payload.model_dump(),
                headers=headers,
            )
            response.raise_for_status()

            logging.debug(
                "Successfully sent batch of %d metrics to Segment", len(metric_records)
            )
        except httpx.HTTPError:
            logging.warning("HTTP error sending metric to Segment", exc_info=True)
        except Exception:
            logging.warning("Unexpected error sending metric to Segment", exc_info=True)

    def close(self) -> None:
        """Close the Segment client and cleanup resources.

        Cancels the worker (its ``CancelledError`` handler sends the final
        batch, then the HTTP client is closed) and waits up to 10 s for it to
        finish. At process exit the observability loop's own shutdown does the
        same for every worker, so this is for callers that close a client
        earlier.

        Marks the client closed first, so a later ``send_metric`` is dropped
        rather than starting a worker nothing would stop.
        """
        if not self.enabled:
            return

        with self._start_lock:
            self._closed = True
            worker = self._worker

        if worker is None or worker.done():
            return
        worker.cancel()
        if observability_loop.in_loop_thread():
            # Waiting here would block the loop the worker needs to finish.
            return
        if not worker.wait(timeout=10.0):
            logging.warning("SegmentClient worker did not terminate within timeout")

    def _should_send_metric(self, metric_record: "MetricRecord") -> bool:
        """Determine if a metric should be sent to Segment.

        Only metrics with the label `send_to_segment: true` will be sent to Segment.
        This provides explicit control over which metrics are forwarded to Segment
        for business analytics.

        Args:
            metric_record (MetricRecord): The metric record to evaluate

        Returns:
            bool: True if metric should be sent (has send_to_segment=true label), False otherwise
        """
        # Check for explicit send_to_segment label
        send_to_segment = metric_record.labels.get("send_to_segment", "").lower()
        return send_to_segment == "true"


# Enabled clients, so a forked child can drop each one's parent-loop worker.
_live_clients: "weakref.WeakSet[SegmentClient]" = weakref.WeakSet()


def _reset_clients_after_fork() -> None:
    for client in list(_live_clients):
        client._reset_after_fork()


register_after_fork(_reset_clients_after_fork)
