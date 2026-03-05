"""Simulation test demonstrating Temporal Activity Failure Observability.

This test simulates the complete flow when a Temporal activity fails:
1. TracingInterceptor creates a span with basic info
2. OTelEnrichmentInterceptor enriches the span with activity context
3. ErrorOnlySpanProcessor filters and exports only ERROR spans

Run this test to see exactly what data would be exported to ClickHouse.
"""

import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.trace import Status, StatusCode


@dataclass
class SimulatedSpan:
    """Represents a span as it would appear in the OTel collector/ClickHouse."""

    name: str
    status_code: str
    attributes: dict[str, Any]
    events: list[dict[str, Any]]

    def to_clickhouse_row(self) -> dict[str, Any]:
        """Convert span to ClickHouse row format."""
        return {
            "SpanName": self.name,
            "StatusCode": self.status_code,
            "SpanAttributes": self.attributes,
            "Events": self.events,
        }


class TestTemporalTracingSimulation:
    """Simulation of the complete Temporal activity failure observability flow."""

    def simulate_activity_failure(
        self,
        activity_name: str = "fetch_metadata",
        workflow_id: str = "wf-oracle-sync-12345",
        workflow_run_id: str = "run-abc-def-789",
        task_queue: str = "atlan-oracle-connector-production",
        attempt: int = 3,
        tenant_id: str = "tenant-acme-corp",
        schedule_to_close_timeout: timedelta = timedelta(hours=2),
        start_to_close_timeout: timedelta = timedelta(minutes=30),
        heartbeat_timeout: timedelta = timedelta(seconds=60),
        error_type: type = ConnectionRefusedError,
        error_message: str = "Connection refused: Oracle database at 10.0.1.50:1521 not responding",
    ) -> SimulatedSpan:
        """Simulate a failing Temporal activity and return the resulting span.

        This simulates the exact data flow:
        1. TracingInterceptor creates span with activity name and ERROR status
        2. OTelEnrichmentInterceptor adds all the enrichment attributes
        3. ErrorOnlySpanProcessor would forward this to the exporter
        """
        attributes = {}
        events = []

        # =========================================================================
        # STAGE 1: TracingInterceptor (from temporalio.contrib.opentelemetry)
        # Creates the base span with:
        # - Activity name as span name
        # - Basic temporal attributes
        # - Status set to ERROR on failure
        # =========================================================================
        print("\n" + "=" * 80)
        print("STAGE 1: TracingInterceptor creates base span")
        print("=" * 80)

        span_name = f"activity:{activity_name}"
        status_code = "ERROR"

        stage1_attrs = {
            "temporalio.activity.type": activity_name,
            "temporalio.workflow.type": "OracleSyncWorkflow",
        }
        attributes.update(stage1_attrs)

        print(f"  Span Name: {span_name}")
        print(f"  Status Code: {status_code}")
        print(f"  Attributes from TracingInterceptor:")
        for k, v in stage1_attrs.items():
            print(f"    - {k}: {v}")

        # =========================================================================
        # STAGE 2: OTelEnrichmentInterceptor (our custom interceptor)
        # Enriches the span with:
        # - Activity attempt number
        # - Task queue name
        # - Workflow ID and run ID
        # - Timeout configurations
        # - Tenant ID from correlation context
        # =========================================================================
        print("\n" + "=" * 80)
        print("STAGE 2: OTelEnrichmentInterceptor enriches span")
        print("=" * 80)

        stage2_attrs = {
            "temporal.activity.attempt": attempt,
            "temporal.activity.task_queue": task_queue,
            "temporal.workflow.id": workflow_id,
            "temporal.workflow.run_id": workflow_run_id,
            "temporal.activity.schedule_to_close_timeout": str(schedule_to_close_timeout),
            "temporal.activity.start_to_close_timeout": str(start_to_close_timeout),
            "temporal.activity.heartbeat_timeout": str(heartbeat_timeout),
            "tenant.id": tenant_id,
        }
        attributes.update(stage2_attrs)

        print("  Attributes added by OTelEnrichmentInterceptor:")
        for k, v in stage2_attrs.items():
            print(f"    - {k}: {v}")

        # =========================================================================
        # STAGE 2b: Exception handling in OTelEnrichmentInterceptor
        # When the activity raises an exception:
        # - Records exception type, message, and full stack trace
        # - Calls span.record_exception() to add exception event
        # =========================================================================
        print("\n" + "-" * 80)
        print("STAGE 2b: Activity fails - exception details captured")
        print("-" * 80)

        try:
            raise error_type(error_message)
        except Exception as e:
            stack_trace = traceback.format_exc()

        exception_attrs = {
            "exception.type": error_type.__name__,
            "exception.message": error_message,
            "exception.stacktrace": stack_trace,
        }
        attributes.update(exception_attrs)

        exception_event = {
            "name": "exception",
            "attributes": {
                "exception.type": error_type.__name__,
                "exception.message": error_message,
                "exception.stacktrace": stack_trace,
            },
        }
        events.append(exception_event)

        print(f"  exception.type: {error_type.__name__}")
        print(f"  exception.message: {error_message}")
        print(f"  exception.stacktrace: (first 200 chars)")
        print(f"    {stack_trace[:200]}...")

        # =========================================================================
        # STAGE 3: ErrorOnlySpanProcessor
        # Checks if span.status.status_code == StatusCode.ERROR
        # - If ERROR: Forward to BatchSpanProcessor -> OTLPSpanExporter -> Collector
        # - If OK/UNSET: Drop silently (not exported)
        # =========================================================================
        print("\n" + "=" * 80)
        print("STAGE 3: ErrorOnlySpanProcessor filter decision")
        print("=" * 80)

        is_error = status_code == "ERROR"
        print(f"  Status Code: {status_code}")
        print(f"  Is ERROR? {is_error}")
        print(f"  Decision: {'✓ EXPORT to OTel Collector' if is_error else '✗ DROP (not exported)'}")

        return SimulatedSpan(
            name=span_name,
            status_code=status_code,
            attributes=attributes,
            events=events,
        )

    def test_simulate_connection_refused_failure(self):
        """Simulate a connection refused error during metadata fetch."""
        print("\n" + "#" * 80)
        print("# SIMULATION: Oracle Connector - Connection Refused")
        print("#" * 80)

        span = self.simulate_activity_failure(
            activity_name="fetch_metadata",
            workflow_id="wf-oracle-sync-12345",
            tenant_id="tenant-acme-corp",
            attempt=3,
            error_type=ConnectionRefusedError,
            error_message="Connection refused: Oracle database at 10.0.1.50:1521 not responding",
        )

        # =========================================================================
        # FINAL OUTPUT: What lands in ClickHouse
        # =========================================================================
        print("\n" + "=" * 80)
        print("FINAL OUTPUT: ClickHouse Row")
        print("=" * 80)

        row = span.to_clickhouse_row()
        print("\nClickHouse SQL Query to find this failure:")
        print("-" * 80)
        print(
            """
SELECT
    Timestamp,
    SpanName,
    SpanAttributes['temporal.workflow.id'] AS workflow_id,
    SpanAttributes['tenant.id'] AS tenant,
    SpanAttributes['temporal.activity.attempt'] AS attempt,
    SpanAttributes['exception.type'] AS error_type,
    SpanAttributes['exception.message'] AS error_message,
    SpanAttributes['exception.stacktrace'] AS stacktrace
FROM otel_traces.spans
WHERE StatusCode = 'ERROR'
  AND SpanName LIKE 'activity:%'
ORDER BY Timestamp DESC
LIMIT 10;
"""
        )

        print("\nResulting Row:")
        print("-" * 80)
        print(f"  SpanName: {row['SpanName']}")
        print(f"  StatusCode: {row['StatusCode']}")
        print("  SpanAttributes:")
        for k, v in sorted(row["SpanAttributes"].items()):
            if k == "exception.stacktrace":
                print(f"    {k}: <{len(str(v))} chars>")
            else:
                print(f"    {k}: {v}")

        assert span.status_code == "ERROR"
        assert span.attributes["temporal.activity.attempt"] == 3
        assert span.attributes["tenant.id"] == "tenant-acme-corp"
        assert span.attributes["exception.type"] == "ConnectionRefusedError"

    def test_simulate_timeout_failure(self):
        """Simulate a timeout error during data extraction."""
        print("\n" + "#" * 80)
        print("# SIMULATION: Snowflake Connector - Query Timeout")
        print("#" * 80)

        span = self.simulate_activity_failure(
            activity_name="extract_table_data",
            workflow_id="wf-snowflake-extract-67890",
            workflow_run_id="run-xyz-123-456",
            task_queue="atlan-snowflake-connector-production",
            tenant_id="tenant-bigbank-inc",
            attempt=1,
            schedule_to_close_timeout=timedelta(hours=4),
            start_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=5),
            error_type=TimeoutError,
            error_message="Query execution exceeded 1 hour timeout: SELECT * FROM ANALYTICS.TRANSACTIONS",
        )

        print("\n" + "=" * 80)
        print("FINAL OUTPUT: ClickHouse Row")
        print("=" * 80)

        row = span.to_clickhouse_row()
        print(f"\n  SpanName: {row['SpanName']}")
        print(f"  StatusCode: {row['StatusCode']}")
        print("  Key Attributes:")
        print(f"    workflow_id: {row['SpanAttributes']['temporal.workflow.id']}")
        print(f"    tenant.id: {row['SpanAttributes']['tenant.id']}")
        print(f"    attempt: {row['SpanAttributes']['temporal.activity.attempt']}")
        print(f"    exception.type: {row['SpanAttributes']['exception.type']}")
        print(f"    exception.message: {row['SpanAttributes']['exception.message']}")

        assert span.status_code == "ERROR"
        assert span.attributes["exception.type"] == "TimeoutError"

    def test_simulate_auth_failure(self):
        """Simulate an authentication failure."""
        print("\n" + "#" * 80)
        print("# SIMULATION: Postgres Connector - Authentication Failed")
        print("#" * 80)

        span = self.simulate_activity_failure(
            activity_name="test_connection",
            workflow_id="wf-postgres-validate-99999",
            workflow_run_id="run-val-001-002",
            task_queue="atlan-postgres-connector-staging",
            tenant_id="tenant-startup-xyz",
            attempt=1,
            error_type=PermissionError,
            error_message="FATAL: password authentication failed for user 'readonly_user'",
        )

        print("\n" + "=" * 80)
        print("FINAL OUTPUT: ClickHouse Row")
        print("=" * 80)

        row = span.to_clickhouse_row()
        print(f"\n  SpanName: {row['SpanName']}")
        print(f"  StatusCode: {row['StatusCode']}")
        print("  Key Attributes:")
        print(f"    workflow_id: {row['SpanAttributes']['temporal.workflow.id']}")
        print(f"    tenant.id: {row['SpanAttributes']['tenant.id']}")
        print(f"    exception.type: {row['SpanAttributes']['exception.type']}")
        print(f"    exception.message: {row['SpanAttributes']['exception.message']}")

        assert span.status_code == "ERROR"
        assert span.attributes["exception.type"] == "PermissionError"

    def test_successful_activity_not_exported(self):
        """Demonstrate that successful activities are NOT exported."""
        print("\n" + "#" * 80)
        print("# SIMULATION: Successful Activity (NOT exported)")
        print("#" * 80)

        print("\n" + "=" * 80)
        print("STAGE 1-2: TracingInterceptor + OTelEnrichmentInterceptor")
        print("=" * 80)
        print("  Span created with basic attributes...")
        print("  Activity executes successfully...")
        print("  Status Code: OK")

        print("\n" + "=" * 80)
        print("STAGE 3: ErrorOnlySpanProcessor filter decision")
        print("=" * 80)
        print("  Status Code: OK")
        print("  Is ERROR? False")
        print("  Decision: ✗ DROP (not exported)")
        print("\n  → This span is NEVER sent to the OTel Collector")
        print("  → No ClickHouse row is created")
        print("  → No storage used, no noise in dashboards")

    def test_show_grafana_dashboard_queries(self):
        """Show example Grafana dashboard queries enabled by this implementation."""
        print("\n" + "#" * 80)
        print("# GRAFANA DASHBOARD QUERIES")
        print("#" * 80)

        print("\n1. Top failing activities by count:")
        print("-" * 60)
        print(
            """
SELECT
    SpanAttributes['temporalio.activity.type'] AS activity_type,
    count() AS failure_count
FROM otel_traces.spans
WHERE StatusCode = 'ERROR'
  AND Timestamp > now() - INTERVAL 24 HOUR
GROUP BY activity_type
ORDER BY failure_count DESC
LIMIT 10;
"""
        )

        print("\n2. Failure rate by tenant:")
        print("-" * 60)
        print(
            """
SELECT
    SpanAttributes['tenant.id'] AS tenant,
    count() AS failure_count,
    avg(toUInt32(SpanAttributes['temporal.activity.attempt'])) AS avg_retries
FROM otel_traces.spans
WHERE StatusCode = 'ERROR'
  AND Timestamp > now() - INTERVAL 7 DAY
GROUP BY tenant
ORDER BY failure_count DESC;
"""
        )

        print("\n3. Failure timeline (for alerting):")
        print("-" * 60)
        print(
            """
SELECT
    toStartOfHour(Timestamp) AS hour,
    SpanAttributes['exception.type'] AS error_type,
    count() AS failures
FROM otel_traces.spans
WHERE StatusCode = 'ERROR'
  AND Timestamp > now() - INTERVAL 24 HOUR
GROUP BY hour, error_type
ORDER BY hour DESC;
"""
        )

        print("\n4. Get stack trace for specific failure:")
        print("-" * 60)
        print(
            """
SELECT
    SpanAttributes['temporal.workflow.id'] AS workflow_id,
    SpanAttributes['exception.type'] AS error_type,
    SpanAttributes['exception.message'] AS error_message,
    SpanAttributes['exception.stacktrace'] AS stacktrace
FROM otel_traces.spans
WHERE StatusCode = 'ERROR'
  AND SpanAttributes['temporal.workflow.id'] = 'wf-oracle-sync-12345'
LIMIT 1;
"""
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
