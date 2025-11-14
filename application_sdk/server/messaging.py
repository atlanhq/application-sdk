"""Message processing functionality for Message Processor input bindings.

This module provides classes for processing messages  via Dapr input bindings,
supporting both per-message and batch processing modes.
"""

import asyncio
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type
from uuid import uuid4

from pydantic import BaseModel, Field

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class MessageProcessorConfig(BaseModel):
    """Configuration for message processor.

    The processing mode is automatically determined by batch_size:
    - batch_size = 1: Per-message processing (immediate)
    - batch_size > 1: Batch processing (accumulate and process)

    Attributes:
        binding_name: Name of the Messaging input binding (must match component metadata.name)
        batch_size: Number of messages to accumulate before processing (1 = per-message mode)
        batch_timeout: Maximum time in seconds to wait before processing accumulated messages
        trigger_workflow: Whether to trigger a Temporal workflow for each message/batch
        workflow_class: Workflow class to trigger (required if trigger_workflow=True)

    Examples:
        >>> # Per-message processing (batch_size=1)
        >>> config = MessageProcessorConfig(
        ...     binding_name="message-processor-input",
        ...     batch_size=1
        ... )

        >>> # Batch processing with workflow trigger
        >>> config = MessageProcessorConfig(
        ...     binding_name="message-processor-input",
        ...     batch_size=50,
        ...     batch_timeout=5.0,
        ...     trigger_workflow=True,
        ...     workflow_class=MyWorkflowClass
        ... )
    """

    binding_name: str = Field(..., description="Name of the Messaging input binding")
    batch_size: int = Field(
        default=1,
        gt=0,
        description="Number of messages per batch (1 = per-message, >1 = batch mode)",
    )
    batch_timeout: float = Field(
        default=5.0,
        gt=0,
        description="Maximum time to wait for batch in seconds (ignored if batch_size=1)",
    )
    trigger_workflow: bool = Field(
        default=False, description="Whether to trigger a workflow for each message/batch"
    )
    workflow_class: Optional[Type[WorkflowInterface]] = Field(
        default=None, description="Workflow class to trigger (if trigger_workflow=True)"
    )

    @property
    def is_batch_mode(self) -> bool:
        """Check if processor is in batch mode.

        Returns:
            bool: True if batch_size > 1, False for per-message mode
        """
        return self.batch_size > 1


class MessageProcessor:
    """Handles message processing from Message Processing input bindings.

    This processor supports two modes:
    - per_message: Process each message immediately as it arrives
    - batch: Accumulate messages and process in batches (based on size or timeout)

    The processor can optionally trigger Temporal workflows for processing, or use a
    custom callback function.

    Attributes:
        config: Processor configuration
        process_callback: Optional custom callback for message processing
        workflow_client: Temporal workflow client for workflow triggering
        batch: Current accumulated batch of messages
        lock: Async lock for thread-safe batch operations
        last_process_time: Timestamp of last batch processing
        is_running: Whether the batch timer is running
        process_task: Background task for batch timeout checking
        total_processed: Total number of messages processed
        total_errors: Total number of processing errors

    Examples:
        >>> # Per-message processing with callback
        >>> async def process_messages(messages: List[dict]):
        ...     for msg in messages:
        ...         print(f"Processing: {msg}")
        ...
        >>> config = MessageProcessorConfig(
        ...     binding_name="message-processor-input",
        ...     process_mode="per_message"
        ... )
        >>> processor = MessageProcessor(config, process_callback=process_messages)
        >>> await processor.start()

        >>> # Batch processing with workflow trigger
        >>> config = MessageProcessorConfig(
        ...     binding_name="message-processor-input",
        ...     batch_size=50,
        ...     batch_timeout=5.0,
        ...     process_mode="batch",
        ...     trigger_workflow=True,
        ...     workflow_class=MyWorkflow
        ... )
        >>> processor = MessageProcessor(config, workflow_client=client)
        >>> await processor.start()
    """

    def __init__(
        self,
        config: MessageProcessorConfig,
        process_callback: Optional[
            Callable[[List[dict]], Coroutine[Any, Any, None]]
        ] = None,
        workflow_client: Optional[WorkflowClient] = None,
    ):
        """Initialize the message processor.

        Args:
            config: Processor configuration
            process_callback: Async callback function for processing messages
            workflow_client: Temporal workflow client (required if trigger_workflow=True)

        Raises:
            ValueError: If workflow_client is not provided when trigger_workflow=True
            ValueError: If workflow_class is not provided when trigger_workflow=True
        """
        self.config = config
        self.process_callback = process_callback
        self.workflow_client = workflow_client
        self.batch: List[tuple[str, dict]] = []  # List of (message_id, message) tuples
        self.lock = asyncio.Lock()
        self.last_process_time = datetime.now()
        self.is_running = False
        self.process_task: Optional[asyncio.Task] = None
        self.total_processed = 0
        self.total_errors = 0
        self.message_futures: Dict[str, asyncio.Future] = {}  # Track message processing completion

        if config.trigger_workflow and not workflow_client:
            raise ValueError("workflow_client is required when trigger_workflow=True")

        if config.trigger_workflow and not config.workflow_class:
            raise ValueError("workflow_class is required when trigger_workflow=True")

    async def start(self):
        """Start the batch processor timer.

        For batch mode (batch_size > 1), starts a background timer that processes batches on timeout.
        For per-message mode (batch_size = 1), this is a no-op.
        """
        if self.config.is_batch_mode and not self.is_running:
            self.is_running = True
            self.process_task = asyncio.create_task(self._batch_timer())
            logger.info(
                f"Message processor started in batch mode "
                f"(size={self.config.batch_size}, timeout={self.config.batch_timeout}s)"
            )

    async def stop(self):
        """Stop the batch processor and process remaining messages.

        Cancels the background timer and processes any remaining messages in the batch.
        """
        self.is_running = False
        if self.process_task:
            self.process_task.cancel()
            try:
                await self.process_task
            except asyncio.CancelledError:
                pass

        if self.batch:
            await self._process_batch()

        logger.info("Message processor stopped")

    async def add_message(self, message: dict) -> dict:
        """Add a message to the processor.

        This method only acknowledges the message after successful processing.
        If processing fails, it retries after 1 second before raising an exception.

        Args:
            message: The message data to process

        Returns:
            dict: Processing result containing status and details

        Raises:
            Exception: If message processing fails after retry
        """
        retry_count = 0
        max_retries = 1
        retry_delay = 1.0  # 1 second

        while retry_count <= max_retries:
            try:
                if not self.config.is_batch_mode:
                    result = await self._process_single_message(message)
                    logger.info("✓ Message processed successfully, acknowledging to Dapr")
                    return result
                else:
                    result = await self._add_to_batch_and_wait(message)
                    logger.info("✓ Message batch processed successfully, acknowledging to Dapr")
                    return result
            except Exception as e:
                retry_count += 1
                self.total_errors += 1
                
                if retry_count <= max_retries:
                    logger.warning(
                        f"✗ Message processing failed (attempt {retry_count}/{max_retries + 1}), "
                        f"retrying in {retry_delay}s: {e}"
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(
                        f"✗ Message processing failed after {max_retries + 1} attempts, "
                        f"NOT acknowledging to Dapr (message will be redelivered): {e}",
                        exc_info=True
                    )
                    # Raise exception to signal Dapr not to acknowledge
                    raise

    async def _process_single_message(self, message: dict) -> dict:
        """Process a single message immediately.

        Args:
            message: The message to process

        Returns:
            dict: Processing result

        Raises:
            Exception: If processing fails
        """
        try:
            logger.info(f"Processing message: {message.get('event_type', 'unknown')}")

            if self.config.trigger_workflow:
                # Trigger workflow for this message
                result = await self._trigger_workflow_for_message(message)
            elif self.process_callback:
                # Use custom callback
                await self.process_callback([message])
                result = {
                    "status": "processed",
                    "message": "Message processed successfully",
                }
            else:
                # Default processing - just log
                logger.info(f"Processed message: {message}")
                result = {"status": "processed", "message": "Message logged"}

            self.total_processed += 1

            # Record success metric
            metrics = get_metrics()
            metrics.record_metric(
                name="messages_processed_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success", "mode": "per_message"},
                description="Total messages processed",
            )

            return result

        except Exception as e:
            logger.error(f"Error processing single message: {e}", exc_info=True)
            self.total_errors += 1

            # Record error metric
            metrics = get_metrics()
            metrics.record_metric(
                name="messages_processed_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error", "mode": "per_message"},
                description="Total messages processed",
            )
            raise

    async def _add_to_batch_and_wait(self, message: dict) -> dict:
        """Add message to batch and wait for processing to complete.

        This ensures the message is only acknowledged after its batch is successfully processed.

        Args:
            message: The message to add

        Returns:
            dict: Status of batch processing

        Raises:
            Exception: If batch processing fails
        """
        message_id = str(uuid4())
        future = asyncio.Future()
        
        async with self.lock:
            # Register future for this message
            self.message_futures[message_id] = future
            
            # Add message with ID to batch
            self.batch.append((message_id, message))
            current_size = len(self.batch)
            
            logger.info(
                f"✓ Received: {message.get('event_type', 'unknown')}/{message.get('event_name', 'unknown')} "
                f"| Data keys: {list(message.get('data', {}).keys())}"
            )
            logger.info(f"  Added to batch (current: {current_size}/{self.config.batch_size})")

            # Process immediately if batch is full
            if current_size >= self.config.batch_size:
                logger.info(
                    f"⚡ Batch full ({current_size}/{self.config.batch_size}), processing now..."
                )
                await self._process_batch()

        # Wait for this message's batch to be processed
        # This will block until _process_batch completes and resolves the future
        try:
            result = await future
            return result
        except Exception as e:
            # Clean up future on error
            async with self.lock:
                self.message_futures.pop(message_id, None)
            raise

    async def _batch_timer(self):
        """Timer to process batch on timeout.

        Runs as a background task checking batch timeout periodically.
        """
        while self.is_running:
            try:
                await asyncio.sleep(self.config.batch_timeout)

                async with self.lock:
                    if self.batch:
                        time_since_last = (
                            datetime.now() - self.last_process_time
                        ).total_seconds()
                        if time_since_last >= self.config.batch_timeout:
                            logger.info(
                                f"Batch timeout reached ({time_since_last:.1f}s), "
                                f"processing {len(self.batch)} messages"
                            )
                            await self._process_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in batch timer: {e}", exc_info=True)

    async def _process_batch(self):
        """Process the current batch (must be called with lock held).

        Resolves or rejects all message futures based on processing result.

        Raises:
            Exception: If batch processing fails
        """
        if not self.batch:
            return

        batch_to_process = self.batch.copy()
        self.batch.clear()
        self.last_process_time = datetime.now()

        # Extract messages and their IDs
        message_ids = [msg_id for msg_id, _ in batch_to_process]
        messages = [msg for _, msg in batch_to_process]

        try:
            logger.info(f"📦 Processing batch of {len(messages)} messages...")

            if self.config.trigger_workflow:
                # Trigger workflow for the batch
                await self._trigger_workflow_for_batch(messages)
            elif self.process_callback:
                # Use custom callback
                await self.process_callback(messages)
            else:
                # Default processing - just log
                for message in messages:
                    logger.info(f"Processed message: {message}")

            self.total_processed += len(messages)

            # Record success metric
            metrics = get_metrics()
            metrics.record_metric(
                name="messages_processed_total",
                value=float(len(messages)),
                metric_type=MetricType.COUNTER,
                labels={"status": "success", "mode": "batch"},
                description="Total messages processed",
            )
            metrics.record_metric(
                name="message_processor_batch_size",
                value=float(len(messages)),
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Message processing batch size distribution",
            )

            logger.info(f"✅ Batch processing completed successfully ({len(messages)} messages)")

            # Resolve all futures with success
            success_result = {
                "status": "processed",
                "message": f"Batch of {len(messages)} messages processed successfully",
                "batch_size": len(messages)
            }
            for msg_id in message_ids:
                future = self.message_futures.pop(msg_id, None)
                if future and not future.done():
                    future.set_result(success_result)

        except Exception as e:
            logger.error(f"❌ Error processing batch: {e}", exc_info=True)
            self.total_errors += 1

            # Record error metric
            metrics = get_metrics()
            metrics.record_metric(
                name="messages_processed_total",
                value=float(len(messages)),
                metric_type=MetricType.COUNTER,
                labels={"status": "error", "mode": "batch"},
                description="Total messages processed",
            )

            # Reject all futures with the error
            for msg_id in message_ids:
                future = self.message_futures.pop(msg_id, None)
                if future and not future.done():
                    future.set_exception(e)

            raise

    async def _trigger_workflow_for_message(self, message: dict) -> dict:
        """Trigger a workflow for a single message.

        Args:
            message: The message to process

        Returns:
            dict: Workflow trigger result containing workflow_id and run_id

        Raises:
            ValueError: If workflow client or class not configured
            Exception: If workflow trigger fails
        """
        if not self.workflow_client or not self.config.workflow_class:
            raise ValueError("Workflow client and class must be configured")

        try:
            workflow_data = await self.workflow_client.start_workflow(
                {"message": message}, workflow_class=self.config.workflow_class
            )

            return {
                "status": "workflow_started",
                "workflow_id": workflow_data.get("workflow_id", ""),
                "run_id": workflow_data.get("run_id", ""),
            }
        except Exception as e:
            logger.error(f"Error triggering workflow for message: {e}", exc_info=True)
            raise

    async def _trigger_workflow_for_batch(self, batch: List[dict]):
        """Trigger a workflow for a batch of messages.

        Args:
            batch: The batch of messages to process

        Raises:
            ValueError: If workflow client or class not configured
            Exception: If workflow trigger fails
        """
        if not self.workflow_client or not self.config.workflow_class:
            raise ValueError("Workflow client and class must be configured")

        try:
            workflow_data = await self.workflow_client.start_workflow(
                {"messages": batch, "batch_size": len(batch)},
                workflow_class=self.config.workflow_class,
            )

            logger.info(
                f"Started workflow for batch: workflow_id={workflow_data.get('workflow_id')}"
            )
        except Exception as e:
            logger.error(f"Error triggering workflow for batch: {e}", exc_info=True)
            raise

    def get_stats(self) -> dict:
        """Get processor statistics.

        Returns:
            dict: Processor statistics including:
                - is_running: Whether processor is active
                - current_batch_size: Current number of messages in batch
                - batch_size_threshold: Configured batch size limit
                - batch_timeout: Configured timeout in seconds
                - is_batch_mode: True if batch_size > 1, False otherwise
                - total_processed: Total messages processed
                - total_errors: Total processing errors
                - time_since_last_process: Seconds since last batch processing
        """
        return {
            "is_running": self.is_running,
            "current_batch_size": len(self.batch),
            "batch_size_threshold": self.config.batch_size,
            "batch_timeout": self.config.batch_timeout,
            "is_batch_mode": self.config.is_batch_mode,
            "total_processed": self.total_processed,
            "total_errors": self.total_errors,
            "time_since_last_process": (
                datetime.now() - self.last_process_time
            ).total_seconds(),
        }

