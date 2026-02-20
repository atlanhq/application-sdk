"""Tests for AtlanParquetObservability intermediate class.

Covers:
- Master toggle (ENABLE_OBSERVABILITY_DAPR_SINK) gating
- Per-signal flag gating in leaf adapters
- Timestamp float→int64 nanosecond conversion in parquet output
- Class hierarchy (all adapters inherit from AtlanParquetObservability)
"""

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Generator, List
from unittest import mock

import pandas as pd
import pytest

from application_sdk.observability.metrics_adaptor import AtlanMetricsAdapter
from application_sdk.observability.models import MetricRecord, MetricType
from application_sdk.observability.observability import (
    AtlanObservability,
    AtlanParquetObservability,
)
from application_sdk.observability.traces_adaptor import AtlanTracesAdapter, TraceRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patched_metrics_adapter() -> Generator[AtlanMetricsAdapter, None, None]:
    """Create a metrics adapter with mocked OTel and env."""
    with mock.patch.dict(
        "os.environ",
        {
            "ENABLE_OTLP_METRICS": "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        },
    ):
        with mock.patch("opentelemetry.metrics.set_meter_provider"):
            with mock.patch("opentelemetry.sdk.metrics.MeterProvider") as mock_provider:
                mock_provider.return_value.get_meter.return_value = mock.MagicMock()
                adapter = AtlanMetricsAdapter()
                yield adapter


@contextmanager
def _patched_traces_adapter() -> Generator[AtlanTracesAdapter, None, None]:
    """Create a traces adapter with mocked OTel and env."""
    with mock.patch.dict(
        "os.environ",
        {
            "ENABLE_OTLP_TRACES": "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        },
    ):
        with mock.patch("opentelemetry.trace.set_tracer_provider"):
            with mock.patch("opentelemetry.sdk.trace.TracerProvider") as mock_provider:
                mock_provider.return_value.get_tracer.return_value = mock.MagicMock()
                adapter = AtlanTracesAdapter()
                yield adapter


def _make_metric_records(n: int = 3) -> List[Dict[str, Any]]:
    ts = datetime(2026, 2, 21, 12, 0, 0).timestamp()
    return [
        MetricRecord(
            timestamp=ts + i,
            name=f"metric_{i}",
            value=float(i),
            type=MetricType.COUNTER,
            labels={"env": "test"},
        ).model_dump()
        for i in range(n)
    ]


def _make_trace_records(n: int = 3) -> List[Dict[str, Any]]:
    ts = datetime(2026, 2, 21, 12, 0, 0).timestamp()
    return [
        TraceRecord(
            timestamp=ts + i,
            trace_id=f"trace_{i}",
            span_id=f"span_{i}",
            name=f"span_{i}",
            kind="SERVER",
            status_code="OK",
            attributes={"test": "value"},
            duration_ms=100.0,
        ).model_dump()
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Class hierarchy
# ---------------------------------------------------------------------------


class TestClassHierarchy:
    """Verify the inheritance chain is correct."""

    def test_metrics_adapter_inherits_parquet(self):
        assert issubclass(AtlanMetricsAdapter, AtlanParquetObservability)

    def test_traces_adapter_inherits_parquet(self):
        assert issubclass(AtlanTracesAdapter, AtlanParquetObservability)

    def test_parquet_observability_inherits_base(self):
        assert issubclass(AtlanParquetObservability, AtlanObservability)

    def test_logger_adapter_inherits_parquet(self):
        from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter

        assert issubclass(AtlanLoggerAdapter, AtlanParquetObservability)


# ---------------------------------------------------------------------------
# Master toggle gating
# ---------------------------------------------------------------------------


class TestMasterToggle:
    """ENABLE_OBSERVABILITY_DAPR_SINK must gate all writes."""

    @pytest.mark.asyncio
    async def test_master_off_skips_flush_metrics(self):
        with _patched_metrics_adapter() as adapter:
            records = _make_metric_records()
            with mock.patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
                False,
            ):
                with mock.patch("pandas.DataFrame.to_parquet") as mock_write:
                    await adapter._flush_records(records)
                    mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_master_off_skips_flush_traces(self):
        with _patched_traces_adapter() as adapter:
            records = _make_trace_records()
            with mock.patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
                False,
            ):
                with mock.patch("pandas.DataFrame.to_parquet") as mock_write:
                    await adapter._flush_records(records)
                    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Per-signal flag gating
# ---------------------------------------------------------------------------


class TestPerSignalFlags:
    """Per-signal flags gate before reaching the parquet layer."""

    @pytest.mark.asyncio
    async def test_metrics_sink_disabled_skips_super(self):
        with _patched_metrics_adapter() as adapter:
            records = _make_metric_records()
            with mock.patch(
                "application_sdk.observability.metrics_adaptor.ENABLE_METRICS_SINK",
                False,
            ):
                with mock.patch.object(
                    AtlanParquetObservability,
                    "_flush_records",
                ) as mock_parent:
                    await adapter._flush_records(records)
                    mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_traces_sink_disabled_skips_super(self):
        with _patched_traces_adapter() as adapter:
            records = _make_trace_records()
            with mock.patch(
                "application_sdk.observability.traces_adaptor.ENABLE_TRACES_SINK",
                False,
            ):
                with mock.patch.object(
                    AtlanParquetObservability,
                    "_flush_records",
                ) as mock_parent:
                    await adapter._flush_records(records)
                    mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_metrics_sink_enabled_calls_super(self):
        with _patched_metrics_adapter() as adapter:
            records = _make_metric_records()
            with mock.patch(
                "application_sdk.observability.metrics_adaptor.ENABLE_METRICS_SINK",
                True,
            ):
                with mock.patch.object(
                    AtlanParquetObservability,
                    "_flush_records",
                ) as mock_parent:
                    await adapter._flush_records(records)
                    mock_parent.assert_called_once_with(records)

    @pytest.mark.asyncio
    async def test_traces_sink_enabled_calls_super(self):
        with _patched_traces_adapter() as adapter:
            records = _make_trace_records()
            with mock.patch(
                "application_sdk.observability.traces_adaptor.ENABLE_TRACES_SINK",
                True,
            ):
                with mock.patch.object(
                    AtlanParquetObservability,
                    "_flush_records",
                ) as mock_parent:
                    await adapter._flush_records(records)
                    mock_parent.assert_called_once_with(records)


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------


class TestTimestampConversion:
    """Parquet files must have timestamp as int64 nanoseconds."""

    @pytest.mark.asyncio
    async def test_timestamp_written_as_int64_nanoseconds(self, tmp_path):
        with _patched_metrics_adapter() as adapter:
            adapter.data_dir = str(tmp_path)
            records = _make_metric_records(1)
            original_ts = records[0]["timestamp"]

            with mock.patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
                True,
            ):
                # Mock uploads to avoid Dapr dependency
                with mock.patch(
                    "application_sdk.observability.metrics_adaptor.ENABLE_METRICS_SINK",
                    True,
                ):
                    with mock.patch(
                        "application_sdk.services.objectstore.ObjectStore.upload_file"
                    ):
                        await adapter._flush_records(records)

            # Find the written parquet file
            parquet_files = list(tmp_path.rglob("*.parquet"))
            assert len(parquet_files) == 1

            df = pd.read_parquet(parquet_files[0])
            assert df["timestamp"].dtype == "int64"

            expected_ns = int(original_ts * 1_000_000_000)
            assert df["timestamp"].iloc[0] == expected_ns

    @pytest.mark.asyncio
    async def test_timestamp_precision_preserved(self, tmp_path):
        """Verify sub-second precision survives the conversion."""
        with _patched_metrics_adapter() as adapter:
            adapter.data_dir = str(tmp_path)
            # Use a timestamp with fractional seconds
            ts = 1740153600.123456
            records = [
                MetricRecord(
                    timestamp=ts,
                    name="precision_test",
                    value=1.0,
                    type=MetricType.COUNTER,
                    labels={"env": "test"},
                ).model_dump()
            ]

            with mock.patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
                True,
            ):
                with mock.patch(
                    "application_sdk.observability.metrics_adaptor.ENABLE_METRICS_SINK",
                    True,
                ):
                    with mock.patch(
                        "application_sdk.services.objectstore.ObjectStore.upload_file"
                    ):
                        await adapter._flush_records(records)

            parquet_files = list(tmp_path.rglob("*.parquet"))
            df = pd.read_parquet(parquet_files[0])
            # Microsecond precision from float should be preserved
            stored_ns = df["timestamp"].iloc[0]
            assert stored_ns == int(ts * 1_000_000_000)
