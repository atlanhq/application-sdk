"""Unit tests for application_sdk.observability.decorators.observability_decorator.

Public contract: ``observability(logger=None, metrics=None, traces=None)`` decorator
that wraps sync/async functions to record traces and metrics for both success and
failure paths.

All real dependencies (logger, metrics, traces) are mocked. No real OTel, no real
threads, no real loops beyond pytest-asyncio's default.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from application_sdk.observability.decorators.observability_decorator import (
    _record_error_observability,
    _record_success_observability,
    observability,
)
from application_sdk.observability.metrics_adaptor import MetricType


def _mock_components():
    """Build a triple of (logger, metrics, traces) mocks."""
    return MagicMock(name="logger"), MagicMock(name="metrics"), MagicMock(name="traces")


# ---------------------------------------------------------------------------
# Sync wrapper paths
# ---------------------------------------------------------------------------


class TestSyncWrapper:
    def test_sync_success_records_trace_and_metric(self) -> None:
        logger, metrics, traces = _mock_components()

        @observability(logger=logger, metrics=metrics, traces=traces)
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

        traces.record_trace.assert_called_once()
        kwargs = traces.record_trace.call_args.kwargs
        assert kwargs["status_code"] == "OK"
        assert kwargs["name"] == "add"
        assert kwargs["kind"] == "INTERNAL"
        assert kwargs["attributes"]["function"] == "add"

        metrics.record_metric.assert_called_once()
        m_kwargs = metrics.record_metric.call_args.kwargs
        assert m_kwargs["name"] == "add_success"
        assert m_kwargs["metric_type"] == MetricType.COUNTER
        assert m_kwargs["value"] == 1

    def test_sync_failure_records_error_and_reraises(self) -> None:
        logger, metrics, traces = _mock_components()

        @observability(logger=logger, metrics=metrics, traces=traces)
        def boom() -> None:
            raise ValueError("kaput")

        with pytest.raises(ValueError, match="kaput"):
            boom()

        traces.record_trace.assert_called_once()
        assert traces.record_trace.call_args.kwargs["status_code"] == "ERROR"
        # PR #1601 (BLDX-1186, BLDX-1187): trace event "error" attribute now
        # carries the exception class name, not the raw message — avoids
        # leaking PII / credentials embedded in the original message.
        events = traces.record_trace.call_args.kwargs["events"]
        assert events[0]["attributes"]["error"] == "ValueError"

        metrics.record_metric.assert_called_once()
        assert metrics.record_metric.call_args.kwargs["name"] == "boom_failure"

    def test_sync_swallows_trace_exception_but_returns_result(self) -> None:
        """A failure inside traces.record_trace must not break the wrapped function."""
        logger, metrics, traces = _mock_components()
        traces.record_trace.side_effect = RuntimeError("trace exporter down")

        @observability(logger=logger, metrics=metrics, traces=traces)
        def double(x: int) -> int:
            return x * 2

        assert double(7) == 14
        # Logger should record the failure (one of the .error() calls).
        assert any(
            "Failed to record trace" in str(call.args[0])
            for call in logger.error.call_args_list
        )

    def test_sync_swallows_metric_exception_but_returns_result(self) -> None:
        logger, metrics, traces = _mock_components()
        metrics.record_metric.side_effect = RuntimeError("metric exporter down")

        @observability(logger=logger, metrics=metrics, traces=traces)
        def echo(x: str) -> str:
            return x

        assert echo("hi") == "hi"
        assert any(
            "Failed to record metric" in str(call.args[0])
            for call in logger.error.call_args_list
        )


# ---------------------------------------------------------------------------
# Async wrapper paths
# ---------------------------------------------------------------------------


class TestAsyncWrapper:
    @pytest.mark.asyncio
    async def test_async_success_records_observability(self) -> None:
        logger, metrics, traces = _mock_components()

        @observability(logger=logger, metrics=metrics, traces=traces)
        async def add(a: int, b: int) -> int:
            return a + b

        assert await add(4, 5) == 9
        traces.record_trace.assert_called_once()
        assert traces.record_trace.call_args.kwargs["status_code"] == "OK"
        metrics.record_metric.assert_called_once()
        assert metrics.record_metric.call_args.kwargs["name"] == "add_success"

    @pytest.mark.asyncio
    async def test_async_failure_records_error_and_reraises(self) -> None:
        logger, metrics, traces = _mock_components()

        @observability(logger=logger, metrics=metrics, traces=traces)
        async def boom() -> None:
            raise RuntimeError("async-kaput")

        with pytest.raises(RuntimeError, match="async-kaput"):
            await boom()
        traces.record_trace.assert_called_once()
        assert traces.record_trace.call_args.kwargs["status_code"] == "ERROR"
        metrics.record_metric.assert_called_once()
        assert metrics.record_metric.call_args.kwargs["name"] == "boom_failure"
        # PR #1601 (BLDX-1186): metric "error" label now carries the
        # exception class name, not the raw message — avoids leaking
        # PII / credentials and prevents high-cardinality blow-up.
        labels = metrics.record_metric.call_args.kwargs["labels"]
        assert labels["error"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_async_swallows_trace_failure_in_error_path(self) -> None:
        logger, metrics, traces = _mock_components()
        traces.record_trace.side_effect = RuntimeError("trace down")

        @observability(logger=logger, metrics=metrics, traces=traces)
        async def boom() -> None:
            raise ValueError("real-error")

        # Original error must still propagate, not the trace recorder error.
        with pytest.raises(ValueError, match="real-error"):
            await boom()
        # Error path's recorder failure was logged.
        assert any(
            "Failed to record error trace" in str(call.args[0])
            for call in logger.error.call_args_list
        )

    @pytest.mark.asyncio
    async def test_async_swallows_metric_failure_in_error_path(self) -> None:
        logger, metrics, traces = _mock_components()
        metrics.record_metric.side_effect = RuntimeError("metric down")

        @observability(logger=logger, metrics=metrics, traces=traces)
        async def boom() -> None:
            raise ValueError("real-error")

        with pytest.raises(ValueError, match="real-error"):
            await boom()
        assert any(
            "Failed to record error metric" in str(call.args[0])
            for call in logger.error.call_args_list
        )


# ---------------------------------------------------------------------------
# Auto-init defaults: when caller passes None, decorator looks up via
# get_logger / get_metrics / get_traces module-level helpers.
# ---------------------------------------------------------------------------


class TestAutoInitialization:
    def test_none_defaults_use_module_level_lookups(self) -> None:
        log_mock = MagicMock(name="auto_logger")
        metrics_mock = MagicMock(name="auto_metrics")
        traces_mock = MagicMock(name="auto_traces")

        with (
            patch(
                "application_sdk.observability.decorators.observability_decorator.get_logger",
                return_value=log_mock,
            ) as mock_get_logger,
            patch(
                "application_sdk.observability.decorators.observability_decorator.get_metrics",
                return_value=metrics_mock,
            ) as mock_get_metrics,
            patch(
                "application_sdk.observability.decorators.observability_decorator.get_traces",
                return_value=traces_mock,
            ) as mock_get_traces,
        ):

            @observability()
            def hello() -> str:
                return "hi"

            assert hello() == "hi"

        mock_get_logger.assert_called_once()
        mock_get_metrics.assert_called_once()
        mock_get_traces.assert_called_once()
        traces_mock.record_trace.assert_called_once()
        metrics_mock.record_metric.assert_called_once()


# ---------------------------------------------------------------------------
# Helper-level smoke tests of the success / error helpers (covers stray
# branches not hit by wrapper tests, e.g. duration calculation).
# ---------------------------------------------------------------------------


class TestRecordHelpers:
    def test_record_success_observability_swallows_double_failure(self) -> None:
        logger, metrics, traces = _mock_components()
        traces.record_trace.side_effect = RuntimeError("trace")
        metrics.record_metric.side_effect = RuntimeError("metric")

        # Must not raise even though both recorders blow up.
        _record_success_observability(
            logger=logger,
            metrics=metrics,
            traces=traces,
            func_name="f",
            func_doc="doc",
            func_module="mod",
            trace_id="t",
            span_id="s",
            start_time=0.0,
        )

    def test_record_error_observability_swallows_double_failure(self) -> None:
        logger, metrics, traces = _mock_components()
        traces.record_trace.side_effect = RuntimeError("trace")
        metrics.record_metric.side_effect = RuntimeError("metric")

        _record_error_observability(
            logger=logger,
            metrics=metrics,
            traces=traces,
            func_name="f",
            func_doc="doc",
            func_module="mod",
            trace_id="t",
            span_id="s",
            start_time=0.0,
            error=ValueError("oops"),
        )


# ---------------------------------------------------------------------------
# Function-metadata behaviour
# ---------------------------------------------------------------------------


class TestFunctionMetadata:
    def test_uses_default_doc_when_none(self) -> None:
        logger, metrics, traces = _mock_components()

        @observability(logger=logger, metrics=metrics, traces=traces)
        def no_doc() -> int:
            return 1

        assert no_doc() == 1
        attrs = traces.record_trace.call_args.kwargs["attributes"]
        # Default doc fallback is "Executing <name>".
        assert attrs["description"] == "Executing no_doc"

    def test_preserves_wrapped_metadata(self) -> None:
        logger, metrics, traces = _mock_components()

        @observability(logger=logger, metrics=metrics, traces=traces)
        def named() -> None:
            """custom doc."""
            return None

        # functools.wraps preserves __name__ and __doc__.
        assert named.__name__ == "named"
        assert named.__doc__ == "custom doc."
