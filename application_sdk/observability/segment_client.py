import asyncio
import base64
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

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

if TYPE_CHECKING:
    pass  # Reserved for future type-checking-only imports


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
    properties: Dict[str, Any] = Field(
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

    Attributes:
        enabled (bool): Whether Segment client is enabled (has valid write key)
        _queue (asyncio.Queue): Queue for pending metric events
        _client (httpx.AsyncClient): Async HTTP client for API calls
        _worker_task (Optional[asyncio.Task]): Background task processing the queue
    """

    def __init__(
        self,
        enabled: bool,
        write_key: str = "",
        api_url: str = "",
        default_user_id: str = "",
        batch_size: int = 0,
        batch_timeout_seconds: float = 0.0,
    ):
        """Initialize Segment client.

        Args:
            enabled: Whether Segment metrics are enabled
            write_key: Segment write key for authentication (defaults to SEGMENT_WRITE_KEY)
            api_url: Segment API URL (defaults to SEGMENT_API_URL)
            default_user_id: Default user ID for events (defaults to SEGMENT_DEFAULT_USER_ID)
            batch_size: Maximum number of events per batch (defaults to SEGMENT_BATCH_SIZE)
            batch_timeout_seconds: Max seconds to wait before sending batch (defaults to SEGMENT_BATCH_TIMEOUT_SECONDS)
        """
        self.enabled = enabled
        self._write_key = write_key or SEGMENT_WRITE_KEY
        self._api_url = api_url or SEGMENT_API_URL
        self._default_user_id = default_user_id or SEGMENT_DEFAULT_USER_ID
        self._batch_size = batch_size or SEGMENT_BATCH_SIZE
        self._batch_timeout_seconds = (
            batch_timeout_seconds or SEGMENT_BATCH_TIMEOUT_SECONDS
        )
        self._queue: Optional[asyncio.Queue] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._initialized_event = threading.Event()

        if not self.enabled or not self._write_key:
            logging.warning(
                "Segment write key not configured - Segment metrics will be disabled"
            )
            self.enabled = False
            return

        # Start background thread with event loop for async operations
        self._start_worker_thread()

        logging.info(
            f"Segment metrics client initialized (batch_size={self._batch_size}, "
            f"batch_timeout={self._batch_timeout_seconds}s)"
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
            self._client = httpx.AsyncClient(timeout=30.0)

            # Signal that initialization is complete
            self._initialized_event.set()

            # Create and run worker task
            self._worker_task = loop.create_task(self._process_queue())

            # Run event loop until task completes
            try:
                loop.run_until_complete(self._worker_task)
            except Exception:
                pass

        self._worker_thread = threading.Thread(target=run_worker, daemon=True)
        self._worker_thread.start()

        # Wait for thread to complete initialization (with timeout)
        if not self._initialized_event.wait(timeout=5.0):
            logging.error(
                "Segment client worker thread failed to initialize within timeout"
            )

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
        except Exception as e:
            logging.warning(f"Failed to queue metric for Segment: {e}")

    async def _process_queue(self) -> None:
        """Background worker that processes metrics from the queue.

        Continuously processes metrics from the queue and sends them to Segment API in batches.
        """
        if not self._queue or not self._client:
            return

        batch: list["MetricRecord"] = []
        last_send_time = asyncio.get_event_loop().time()

        while True:
            try:
                # Calculate remaining time until batch timeout
                current_time = asyncio.get_event_loop().time()
                time_since_last_send = current_time - last_send_time
                timeout = max(0.1, self._batch_timeout_seconds - time_since_last_send)

                # Get metric from queue (with timeout)
                try:
                    metric_record = await asyncio.wait_for(
                        self._queue.get(), timeout=timeout
                    )
                    batch.append(metric_record)
                    self._queue.task_done()
                except asyncio.TimeoutError:
                    # Timeout - send batch if we have any events
                    pass

                current_time = asyncio.get_event_loop().time()

                # Send batch if we've reached batch size or timeout
                should_send = len(batch) >= self._batch_size or (
                    len(batch) > 0
                    and (current_time - last_send_time) >= self._batch_timeout_seconds
                )

                if should_send and batch:
                    await self._send_batch_to_segment(batch)
                    batch = []
                    last_send_time = asyncio.get_event_loop().time()

            except asyncio.CancelledError:
                # Worker task cancelled - send remaining batch before exit
                if batch:
                    try:
                        await self._send_batch_to_segment(batch)
                    except Exception as e:
                        logging.warning(f"Error sending final batch: {e}")
                break
            except Exception as e:
                logging.warning(f"Error processing Segment metric queue: {e}")

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
            **metric_record.labels,
        }

        # Add optional fields if present
        if metric_record.description:
            event_properties["description"] = metric_record.description
        if metric_record.unit:
            event_properties["unit"] = metric_record.unit

        # Create and return Pydantic model
        return SegmentTrackEvent(
            userId=self._default_user_id,
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
                f"Successfully sent batch of {len(metric_records)} metrics to Segment"
            )
        except httpx.HTTPError as e:
            logging.warning(f"HTTP error sending metric to Segment: {e}")
        except Exception as e:
            logging.warning(f"Unexpected error sending metric to Segment: {e}")

    def close(self) -> None:
        """Close the Segment client and cleanup resources.

        Stops the worker task and closes the HTTP client.
        This method ensures proper cleanup of all resources including:
        - Worker thread and event loop
        - httpx.AsyncClient connection pools
        """
        if not self.enabled:
            return

        # Cancel worker task if running
        if self._loop and self._worker_task and not self._worker_task.done():
            try:
                self._loop.call_soon_threadsafe(self._worker_task.cancel)
            except Exception as e:
                logging.warning(f"Error cancelling worker task: {e}")

        # Close httpx client
        if self._loop and self._client:
            try:
                # Schedule client close in the event loop
                async def close_client():
                    if self._client:
                        await self._client.aclose()

                if self._loop.is_running():
                    # If loop is running, schedule the close
                    future = asyncio.run_coroutine_threadsafe(
                        close_client(), self._loop
                    )
                    # Wait for close to complete (with timeout)
                    try:
                        future.result(timeout=2.0)
                    except Exception as e:
                        logging.warning(f"Timeout or error closing httpx client: {e}")
                else:
                    # If loop is not running, run it directly
                    self._loop.run_until_complete(close_client())
            except Exception as e:
                logging.warning(f"Error closing httpx client: {e}")

        # Wait for worker thread to finish (with timeout)
        if self._worker_thread and self._worker_thread.is_alive():
            try:
                self._worker_thread.join(timeout=2.0)
                if self._worker_thread.is_alive():
                    logging.warning(
                        "SegmentClient worker thread did not terminate within timeout"
                    )
            except Exception as e:
                logging.warning(f"Error joining worker thread: {e}")

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
