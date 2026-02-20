#!/usr/bin/env python3
"""
Test script for dual OTEL export functionality.

This script tests that logs are sent to both:
1. Primary collector (port 4317) - simulates DaemonSet
2. Workflow logs collector (port 5317) - simulates tenant-level collector

Usage:
    # Start the collectors first
    cd poc/dual-export-test
    docker-compose up -d

    # Then run this test (from application-sdk root)
    cd /path/to/application-sdk
    source .venv/bin/activate

    # Set environment variables
    export ENABLE_OTLP_LOGS=true
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    export OTEL_WORKFLOW_LOGS_ENDPOINT=http://localhost:5317

    # Run the test
    python poc/dual-export-test/test_dual_export.py

    # Check logs in both collectors:
    docker logs primary-collector
    docker logs workflow-logs-collector

    # Check S3 (MinIO) for archived logs:
    # Open http://localhost:9001 (login: minioadmin/minioadmin)
    # Browse to workflow-logs bucket
"""

import os
import time

# Set environment variables BEFORE importing the SDK
os.environ["ENABLE_OTLP_LOGS"] = "true"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
os.environ["OTEL_WORKFLOW_LOGS_ENDPOINT"] = os.environ.get(
    "OTEL_WORKFLOW_LOGS_ENDPOINT", "http://localhost:5317"
)

# Disable Dapr sink for this test
os.environ["ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK"] = "false"

print("=" * 60)
print("Dual Export Test")
print("=" * 60)
print(f"ENABLE_OTLP_LOGS: {os.environ.get('ENABLE_OTLP_LOGS')}")
print(f"OTEL_EXPORTER_OTLP_ENDPOINT: {os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')}")
print(f"OTEL_WORKFLOW_LOGS_ENDPOINT: {os.environ.get('OTEL_WORKFLOW_LOGS_ENDPOINT')}")
print("=" * 60)

# Now import the SDK logger
from application_sdk.observability.logger_adaptor import get_logger  # noqa: E402

logger = get_logger("dual-export-test")

print("\nSending test logs...")

# Send some test logs
for i in range(5):
    logger.info(
        f"Test log message {i+1}/5",
        workflow_id="test-workflow-123",
        run_id="test-run-456",
    )
    print(f"  Sent log {i+1}/5")
    time.sleep(0.5)

logger.warning("This is a warning message", workflow_id="test-workflow-123")
logger.error("This is an error message", workflow_id="test-workflow-123")

print("\nWaiting for logs to be exported (10 seconds)...")
time.sleep(10)

print("\n" + "=" * 60)
print("Test complete!")
print("=" * 60)
print("""
Next steps:
1. Check primary collector logs:
   docker logs primary-collector

2. Check workflow logs collector:
   docker logs workflow-logs-collector

3. Check MinIO for S3 files:
   Open http://localhost:9001 (login: minioadmin/minioadmin)
   Browse to 'workflow-logs' bucket -> 'logs' folder

If you see logs in BOTH collectors, dual export is working!
""")
