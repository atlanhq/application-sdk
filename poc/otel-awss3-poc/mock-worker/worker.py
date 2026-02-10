#!/usr/bin/env python3
"""
Mock Temporal Worker that sends logs to OTEL collector.

This simulates how the application_sdk sends logs:
- Uses OTLPLogExporter with gRPC protocol
- Uses BatchLogRecordProcessor for batching
- Includes workflow context attributes: workflow_id, run_id, activity_id, trace_id
- Includes app_name attribute for partitioning

Environment variables:
- OTEL_EXPORTER_OTLP_ENDPOINT: OTEL collector endpoint (default: http://localhost:4317)
- SERVICE_NAME: Service name for this worker (default: mock-worker)
- WORKFLOW_ID: Workflow ID for all logs (default: auto-generated)
- TRACE_ID: Trace ID (usually same as workflow_id)
- RUN_ID: Run ID for the workflow
- LOG_INTERVAL_MS: Interval between logs in milliseconds (default: 1000)
"""

import logging
import os
import random
import socket
import sys
import time
import uuid
from datetime import datetime

from opentelemetry import _logs
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource


# Configuration from environment
OTEL_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "mock-worker")
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")
WORKFLOW_ID = os.environ.get("WORKFLOW_ID", f"workflow-{uuid.uuid4().hex[:8]}")
TRACE_ID = os.environ.get("TRACE_ID", WORKFLOW_ID)  # Default to workflow_id
RUN_ID = os.environ.get("RUN_ID", f"run-{uuid.uuid4().hex[:8]}")
WORKER_ID = os.environ.get("WORKER_ID", socket.gethostname())
LOG_INTERVAL_MS = int(os.environ.get("LOG_INTERVAL_MS", "1000"))

# Batch processing configuration
OTEL_BATCH_DELAY_MS = int(os.environ.get("OTEL_BATCH_DELAY_MS", "1000"))
OTEL_BATCH_SIZE = int(os.environ.get("OTEL_BATCH_SIZE", "512"))
OTEL_QUEUE_SIZE = int(os.environ.get("OTEL_QUEUE_SIZE", "2048"))


def setup_logging():
    """Set up OTEL logging with configuration matching application_sdk."""
    # Create resource attributes (service-level)
    resource = Resource.create({
        "service.name": SERVICE_NAME,
        "service.version": SERVICE_VERSION,
        "host.name": socket.gethostname(),
        "worker.id": WORKER_ID,
    })

    # Create the OTLP exporter (gRPC)
    exporter = OTLPLogExporter(
        endpoint=OTEL_ENDPOINT,
        insecure=True,
    )

    # Create logger provider with batch processor
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            exporter,
            schedule_delay_millis=OTEL_BATCH_DELAY_MS,
            max_export_batch_size=OTEL_BATCH_SIZE,
            max_queue_size=OTEL_QUEUE_SIZE,
        )
    )

    # Set the global logger provider
    _logs.set_logger_provider(logger_provider)

    # Create a logging handler that sends logs to OTEL
    handler = LoggingHandler(
        level=logging.DEBUG,
        logger_provider=logger_provider,
    )

    # Configure the root logger
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            handler,
        ],
    )

    return logging.getLogger(SERVICE_NAME)


class MockTemporalWorker:
    """Simulates a Temporal worker that processes activities and sends logs."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.activity_count = 0
        self.log_count = 0

    def _get_extra_attributes(self, activity_id: str = None) -> dict:
        """Get extra attributes to include with each log (matching SDK behavior)."""
        attrs = {
            # Required for partitioning
            "app_name": SERVICE_NAME,
            # Workflow context
            "trace_id": TRACE_ID,
            "workflow_id": WORKFLOW_ID,
            "run_id": RUN_ID,
            "worker_id": WORKER_ID,
            # Atlan-specific headers (simulated)
            "atlan-request-id": str(uuid.uuid4()),
            "atlan-argo-workflow-name": WORKFLOW_ID,
        }
        if activity_id:
            attrs["activity_id"] = activity_id
            attrs["activity_type"] = "mock_activity"
        return attrs

    def log_workflow_start(self):
        """Log workflow start event."""
        self.logger.info(
            f"Workflow started: {WORKFLOW_ID}",
            extra=self._get_extra_attributes(),
        )

    def log_activity_start(self, activity_name: str) -> str:
        """Log activity start and return activity ID."""
        activity_id = f"activity-{self.activity_count}-{uuid.uuid4().hex[:6]}"
        self.activity_count += 1

        self.logger.info(
            f"Activity started: {activity_name}",
            extra=self._get_extra_attributes(activity_id),
        )
        return activity_id

    def log_activity_progress(self, activity_id: str, message: str, level: str = "INFO"):
        """Log activity progress."""
        log_method = getattr(self.logger, level.lower(), self.logger.info)
        log_method(
            message,
            extra=self._get_extra_attributes(activity_id),
        )
        self.log_count += 1

    def log_activity_complete(self, activity_id: str, activity_name: str, success: bool = True):
        """Log activity completion."""
        if success:
            self.logger.info(
                f"Activity completed successfully: {activity_name}",
                extra=self._get_extra_attributes(activity_id),
            )
        else:
            self.logger.error(
                f"Activity failed: {activity_name}",
                extra=self._get_extra_attributes(activity_id),
            )

    def log_workflow_complete(self, success: bool = True):
        """Log workflow completion."""
        if success:
            self.logger.info(
                f"Workflow completed successfully: {WORKFLOW_ID}",
                extra=self._get_extra_attributes(),
            )
        else:
            self.logger.error(
                f"Workflow failed: {WORKFLOW_ID}",
                extra=self._get_extra_attributes(),
            )

    def simulate_activity(self, activity_name: str, num_logs: int = 5):
        """Simulate an activity with multiple log messages."""
        activity_id = self.log_activity_start(activity_name)

        # Simulate activity work with various log messages
        log_messages = [
            ("Processing started", "INFO"),
            ("Fetching data from source", "INFO"),
            ("Data validation in progress", "DEBUG"),
            ("Transforming records", "INFO"),
            ("Writing results", "INFO"),
            ("Encountered recoverable error, retrying", "WARNING"),
            ("Processing batch complete", "INFO"),
        ]

        for i in range(num_logs):
            msg, level = random.choice(log_messages)
            msg_with_detail = f"{msg} (step {i + 1}/{num_logs}, records: {random.randint(100, 10000)})"
            self.log_activity_progress(activity_id, msg_with_detail, level)
            time.sleep(LOG_INTERVAL_MS / 1000)

        # Occasionally simulate errors
        success = random.random() > 0.1  # 90% success rate
        self.log_activity_complete(activity_id, activity_name, success)
        return success


def main():
    """Main entry point."""
    print(f"Starting mock worker...")
    print(f"  OTEL Endpoint: {OTEL_ENDPOINT}")
    print(f"  Service Name: {SERVICE_NAME}")
    print(f"  Workflow ID: {WORKFLOW_ID}")
    print(f"  Trace ID: {TRACE_ID}")
    print(f"  Run ID: {RUN_ID}")
    print(f"  Worker ID: {WORKER_ID}")
    print(f"  Log Interval: {LOG_INTERVAL_MS}ms")
    print()

    # Wait for collector to be ready
    print("Waiting for collector to be ready...")
    time.sleep(5)

    logger = setup_logging()
    worker = MockTemporalWorker(logger)

    # Simulate a workflow execution
    print("Starting workflow simulation...")
    worker.log_workflow_start()

    # Activity sequence that mimics a real connector workflow
    activities = [
        ("FetchMetadata", 3),
        ("ExtractSchema", 5),
        ("SyncTables", 10),
        ("TransformData", 7),
        ("LoadToDestination", 5),
        ("ValidateResults", 3),
    ]

    all_success = True
    for activity_name, num_logs in activities:
        print(f"\nSimulating activity: {activity_name}")
        success = worker.simulate_activity(activity_name, num_logs)
        if not success:
            all_success = False
            print(f"  Activity {activity_name} failed!")
        else:
            print(f"  Activity {activity_name} completed successfully")

        # Small delay between activities
        time.sleep(1)

    worker.log_workflow_complete(all_success)

    print(f"\nWorkflow simulation complete!")
    print(f"  Total activities: {worker.activity_count}")
    print(f"  Total logs sent: {worker.log_count}")

    # Keep running to allow additional log streaming tests
    print("\nEntering continuous log mode (Ctrl+C to stop)...")
    try:
        while True:
            activity_id = f"continuous-{uuid.uuid4().hex[:6]}"
            worker.log_activity_progress(
                activity_id,
                f"Heartbeat log at {datetime.now().isoformat()}",
                "DEBUG",
            )
            time.sleep(LOG_INTERVAL_MS / 1000 * 2)
    except KeyboardInterrupt:
        print("\nStopping mock worker...")

    # Flush logs before exit
    time.sleep(2)
    print("Mock worker stopped.")


if __name__ == "__main__":
    main()
