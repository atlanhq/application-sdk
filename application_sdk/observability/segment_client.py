import asyncio
import base64
import logging
import threading
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
from application_sdk.observability.models import MetricRecord
from application_sdk.version import __version__


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
    asynchronously, avoiding blocking operations and thread creation overhead.

    The client is automatically enabled when a valid write key is provided.
    No separate boolean flag is needed - the presence of the key determines
    whether events are sent.

    Attributes:
        enabled (bool): Whether Segment client is enabled (has valid write key)
        _queue (asyncio.Queue): Queue for pending metric events
        _client (httpx.AsyncClient): Async HTTP client for API calls
        _worker_task (Optional[asyncio.Task]): Background task processing the queue
    """

    def __init__(
        self,
        write_key: str = "",
        api_url: str = "",
        default_user_id: str = "",
        batch_size: int = 0,
        batch_timeout_seconds: float = 0.0,
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
        """
        self._write_key = write_key or SEGMENT_WRITE_KEY
        self._api_url = api_url or SEGMENT_API_URL
        self._default_user_id = default_user_id or SEGMENT_DEFAULT_USER_ID
        self._batch_size = batch_size or SEGMENT_BATCH_SIZE
        self._batch_timeout_seconds = (
            batch_timeout_seconds or SEGMENT_BATCH_TIMEOUT_SECONDS
        )
        self._queue: asyncio.Queue | None = None
        self._client: httpx.AsyncClient | None = None
        self._worker_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_thread: threading.Thread | None = None
        self._initialized_event = threading.Event()

        # Enable client if write key is present
        self.enabled = bool(self._write_key)

        if not self.enabled:
            logging.warning(
                "Segment write key not configured - Segment metrics will be disabled"
            )
            return

        # Start background thread with event loop for async operations
        self._start_worker_thread()

        logging.info(
            "Segment metrics client initialized (batch_size=%d, batch_timeout=%ss)",
            self._batch_size,
            self._batch_timeout_seconds,
        )

    def _start_worker_thread(self) -> None:
        """Start a background thread with event loop for async operations."""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        # Clear initialization event before starting new thread
        # This ensures we wait for the new thread to actually initialize
        self._initialized_event.clear()

        # Reset references to prevent using stale objects from dead thread
        self._loop = None
        self._queue = None
        self._client = None
        self._worker_task = None

        def run_worker():
            """Run async worker in dedicated thread."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            # Initialize queue and client in the event loop
            self._queue = asyncio.Queue()
            from application_sdk.constants import (  # noqa: PLC0415 — circular: observability is imported transitively by many modules; lifting risks circles
                _HTTP_POOL_TIMEOUT_SECONDS,
            )

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, pool=_HTTP_POOL_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30.0,
                ),
            )

            # Signal that initialization is complete
            self._initialized_event.set()

            # Create and run worker task
            self._worker_task = loop.create_task(self._process_queue())

            # Run event loop until task completes, then tear down the httpx
            # client in the same thread that created it. Doing teardown here
            # (rather than in close()) guarantees the CancelledError handler
            # has finished sending the final batch before the client closes.
            try:
                loop.run_until_complete(self._worker_task)
            except Exception:
                logging.warning(
                    "Segment worker thread loop exited with exception", exc_info=True
                )
            finally:
                if self._client:
                    try:
                        loop.run_until_complete(self._client.aclose())
                    except Exception:
                        logging.warning("Error closing httpx client", exc_info=True)
                loop.close()

        self._worker_thread = threading.Thread(target=run_worker, daemon=True)
        self._worker_thread.start()

        # Wait for thread to complete initialization (with timeout)
        if not self._initialized_event.wait(timeout=5.0):
            logging.error(
                "Segment client worker thread failed to initialize within timeout"
            )

    async def flush(self) -> None:
        """Flush queued events to Segment.

        Drains the asyncio queue and sends any queued events immediately.
        Events already dequeued by the worker into its in-flight batch are
        sent when close() cancels the worker task — call close() after flush()
        for a complete drain.
        Bridges to the worker's dedicated event loop via asyncio.wrap_future.
        No-op if the client is disabled or the worker loop is not running.
        Times out after 10 s to keep shutdown bounded (matches the
        Pushgateway per-call budget; two 10 s calls fit within K8s'
        default 30 s terminationGracePeriodSeconds).
        """
        if not self.enabled or not self._loop or not self._queue:
            return
        if not self._loop.is_running():
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
        if not self.enabled:
            return

        # Check if metric should be sent (allow list filtering)
        if not self._should_send_metric(metric_record):
            return

        # Ensure worker thread is running
        if not self._worker_thread or not self._worker_thread.is_alive():
            self._start_worker_thread()

        # Wait for initialization to complete before sending metrics
        if not self._initialized_event.wait(timeout=1.0):
            return

        if not self._loop or not self._queue:
            return

        try:
            # Schedule coroutine in the worker thread's event loop
            asyncio.run_coroutine_threadsafe(self._queue.put(metric_record), self._loop)
        except Exception:
            logging.warning("Failed to queue metric for Segment", exc_info=True)

    async def _process_queue(self) -> None:
        """Background worker that processes metrics from the queue.

        Continuously processes metrics from the queue and sends them to Segment API in batches.
        """
        if not self._queue or not self._client:
            return

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
                    metric_record = await asyncio.wait_for(
                        self._queue.get(), timeout=timeout
                    )
                    batch.append(metric_record)
                    self._queue.task_done()
                except TimeoutError:  # conformance: ignore[E002,E014] queue.get timeout = flush the partial batch now
                    pass

                current_time = asyncio.get_running_loop().time()

                # Send batch if we've reached batch size or timeout
                should_send = len(batch) >= self._batch_size or (
                    len(batch) > 0
                    and (current_time - last_send_time) >= self._batch_timeout_seconds
                )

                if should_send and batch:
                    await self._send_batch_to_segment(batch)
                    batch = []
                    last_send_time = asyncio.get_running_loop().time()

            except asyncio.CancelledError:
                # Worker task cancelled - send remaining batch before exit
                if batch:
                    try:
                        await self._send_batch_to_segment(batch)
                    except Exception:
                        logging.warning("Error sending final batch", exc_info=True)
                break
            except Exception:
                logging.warning("Error processing Segment metric queue", exc_info=True)

    async def _flush_queue(self) -> None:
        """Drain the queue and send all pending events as a single batch.

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
        self, metric_records: list["MetricRecord"]
    ) -> None:
        """Send a batch of metrics to Segment API.

        Args:
            metric_records: List of metric records to send
        """
        if not self._client or not metric_records:
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
            response = await self._client.post(
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

        Cancels the worker task (allowing the CancelledError handler to send
        the final batch) then waits for the worker thread to exit. The httpx
        client is closed inside the worker thread after the final batch is sent,
        ensuring the HTTP connection pool is never torn down mid-request.
        """
        if not self.enabled:
            return

        # Signal cancellation; the CancelledError handler in _process_queue
        # sends any remaining batch before the task exits.
        if self._loop and self._worker_task and not self._worker_task.done():
            try:
                self._loop.call_soon_threadsafe(self._worker_task.cancel)
            except Exception:
                logging.warning("Error cancelling worker task", exc_info=True)

        # Wait for the worker thread to finish — guarantees the final batch
        # has been sent and the httpx client closed before we return.
        if self._worker_thread and self._worker_thread.is_alive():
            try:
                self._worker_thread.join(timeout=10.0)
                if self._worker_thread.is_alive():
                    logging.warning(
                        "SegmentClient worker thread did not terminate within timeout"
                    )
            except Exception:
                logging.warning("Error joining worker thread", exc_info=True)

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
