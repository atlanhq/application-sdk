import asyncio
import base64
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import httpx

from application_sdk.constants import SEGMENT_API_URL, SEGMENT_WRITE_KEY

if TYPE_CHECKING:
    from application_sdk.observability.metrics_adaptor import MetricRecord


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

    def __init__(self):
        """Initialize Segment client.

        Checks for SEGMENT_WRITE_KEY and sets up async client and queue if enabled.
        """
        self.enabled = bool(SEGMENT_WRITE_KEY)
        self._queue: Optional[asyncio.Queue] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._initialized_event = threading.Event()

        if not self.enabled:
            logging.warning(
                "SEGMENT_WRITE_KEY not configured - Segment metrics will be disabled"
            )
            return

        # Start background thread with event loop for async operations
        self._start_worker_thread()

        logging.info("Segment metrics client initialized successfully")

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

        Continuously processes metrics from the queue and sends them to Segment API.
        """
        if not self._queue or not self._client:
            return

        while True:
            try:
                # Get metric from queue (with timeout to allow graceful shutdown)
                try:
                    metric_record = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # Timeout is normal - continue loop
                    continue

                # Send metric to Segment
                await self._send_to_segment(metric_record)
                self._queue.task_done()

            except asyncio.CancelledError:
                # Worker task cancelled - exit gracefully
                break
            except Exception as e:
                logging.warning(f"Error processing Segment metric queue: {e}")

    async def _send_to_segment(self, metric_record: "MetricRecord") -> None:
        """Send a single metric to Segment API.

        Args:
            metric_record (MetricRecord): Metric record to send
        """
        if not self._client:
            return

        try:
            # Build Segment event payload
            event_properties = {
                "value": metric_record.value,
                "metric_type": metric_record.type.value,  # Convert MetricType enum to string
                **metric_record.labels,
            }

            # Add optional fields if present
            if metric_record.description:
                event_properties["description"] = metric_record.description
            if metric_record.unit:
                event_properties["unit"] = metric_record.unit

            payload = {
                "userId": "atlan.automation",
                "event": metric_record.name,
                "properties": event_properties,
                "timestamp": datetime.fromtimestamp(
                    metric_record.timestamp
                ).isoformat(),
            }

            # Create Basic Auth header
            segment_write_key_encoded = base64.b64encode(
                (SEGMENT_WRITE_KEY + ":").encode("ascii")
            ).decode()

            headers = {
                "content-type": "application/json",
                "Authorization": f"Basic {segment_write_key_encoded}",
            }

            # Send HTTP request
            await self._client.post(SEGMENT_API_URL, json=payload, headers=headers)
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
