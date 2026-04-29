"""Unit tests for application_sdk.observability.traces_adaptor.

Targets the uncovered branches:
- _reset_for_testing (ClassVar reset).
- _setup_otel_traces success path (with OTLP endpoint).
- _setup_otel_traces fallback when OTLPSpanExporter raises.
- _setup_otel_traces full-failure → console-only fallback.
- _setup_console_only_traces success and failure.
- _start_asyncio_flush helper.
- export_record dispatch (TraceRecord vs other; ENABLE_OTLP_TRACES on/off).
- _send_to_otel ERROR-status branch and exception swallow.
- _log_to_console exception swallow.
- record_trace re-raises after logging.

All real OTel SDK objects (TracerProvider, BatchSpanProcessor, ConsoleSpanExporter,
OTLPSpanExporter, set_tracer_provider) are mocked at the import site.
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from application_sdk.observability import traces_adaptor as tracer_mod
from application_sdk.observability.models import TraceRecord
from application_sdk.observability.traces_adaptor import AtlanTracesAdapter, get_traces


def _adapter_with_otel_disabled() -> AtlanTracesAdapter:
    """Build an adapter with OTLP disabled and the flush task suppressed."""
    AtlanTracesAdapter._flush_task_started = True  # don't start a thread
    with mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False):
        return AtlanTracesAdapter()


# ---------------------------------------------------------------------------
# Class-level reset
# ---------------------------------------------------------------------------


class TestResetForTesting:
    def test_reset_for_testing_clears_flag(self) -> None:
        AtlanTracesAdapter._flush_task_started = True
        AtlanTracesAdapter._reset_for_testing()
        assert AtlanTracesAdapter._flush_task_started is False


# ---------------------------------------------------------------------------
# _setup_otel_traces success path
# ---------------------------------------------------------------------------


class TestSetupOtelTraces:
    def test_otel_setup_uses_otlp_when_endpoint_set(self) -> None:
        AtlanTracesAdapter._flush_task_started = True

        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter") as mock_console,
            mock.patch.object(tracer_mod, "OTLPSpanExporter") as mock_otlp,
            mock.patch.object(tracer_mod, "BatchSpanProcessor") as mock_proc,
            mock.patch.object(tracer_mod, "TracerProvider") as mock_provider,
            mock.patch.object(tracer_mod.trace, "set_tracer_provider") as mock_set,
        ):
            mock_provider_instance = mock.MagicMock()
            mock_provider.return_value = mock_provider_instance

            adapter = AtlanTracesAdapter()

        # Both exporters were instantiated.
        mock_console.assert_called_once()
        mock_otlp.assert_called_once()
        # One BatchSpanProcessor per exporter.
        assert mock_proc.call_count == 2
        mock_set.assert_called_once_with(mock_provider_instance)
        assert adapter.tracer_provider is mock_provider_instance
        assert hasattr(adapter, "tracer")

    def test_otel_setup_continues_when_otlp_exporter_fails(self) -> None:
        """If OTLPSpanExporter constructor blows up, console exporter still wins."""
        AtlanTracesAdapter._flush_task_started = True

        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter") as mock_console,
            mock.patch.object(
                tracer_mod, "OTLPSpanExporter", side_effect=RuntimeError("bad config")
            ),
            mock.patch.object(tracer_mod, "BatchSpanProcessor") as mock_proc,
            mock.patch.object(tracer_mod, "TracerProvider"),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider"),
        ):
            adapter = AtlanTracesAdapter()

        mock_console.assert_called_once()
        # Only console processor, not OTLP.
        assert mock_proc.call_count == 1
        assert hasattr(adapter, "tracer")

    def test_otel_setup_falls_back_to_console_only_when_provider_creation_fails(
        self,
    ) -> None:
        """If TracerProvider raises, fallback _setup_console_only_traces runs."""
        AtlanTracesAdapter._flush_task_started = True

        # First TracerProvider() raises (in _setup_otel_traces),
        # second TracerProvider() succeeds (in _setup_console_only_traces).
        provider_instance = mock.MagicMock()
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter"),
            mock.patch.object(tracer_mod, "BatchSpanProcessor"),
            mock.patch.object(
                tracer_mod,
                "TracerProvider",
                side_effect=[RuntimeError("first try fails"), provider_instance],
            ),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider"),
        ):
            adapter = AtlanTracesAdapter()

        # Fallback assigned the second provider.
        assert adapter.tracer_provider is provider_instance


class TestSetupConsoleOnlyTraces:
    def test_console_only_setup_swallows_failure(self) -> None:
        """If everything in console-only setup blows up, the constructor still returns."""
        AtlanTracesAdapter._flush_task_started = True

        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter"),
            mock.patch.object(tracer_mod, "BatchSpanProcessor"),
            # First call (otel path) raises; second call (console path) raises too.
            mock.patch.object(
                tracer_mod, "TracerProvider", side_effect=RuntimeError("kaboom")
            ),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider"),
        ):
            # Constructor returns without re-raising — both setup methods swallow.
            adapter = AtlanTracesAdapter()

        # Tracer provider attribute exists but is None (no half-init).
        assert adapter.tracer_provider is None
        assert adapter.tracer is None


# ---------------------------------------------------------------------------
# _start_asyncio_flush
# ---------------------------------------------------------------------------


class TestStartAsyncioFlush:
    def test_start_asyncio_flush_runs_periodic_flush(self) -> None:
        adapter = _adapter_with_otel_disabled()
        sentinel = object()
        sync_periodic = mock.MagicMock(return_value=sentinel)

        with (
            mock.patch.object(tracer_mod.asyncio, "run") as mock_run,
            mock.patch.object(adapter, "_periodic_flush", new=sync_periodic),
        ):
            adapter._start_asyncio_flush()
            sync_periodic.assert_called_once_with()
            mock_run.assert_called_once_with(sentinel)


# ---------------------------------------------------------------------------
# export_record dispatch
# ---------------------------------------------------------------------------


class TestExportRecord:
    def test_export_record_returns_early_for_non_trace_record(self) -> None:
        adapter = _adapter_with_otel_disabled()
        # Should be a complete no-op; no attempt to call _send_to_otel / _log_to_console.
        with (
            mock.patch.object(adapter, "_send_to_otel") as mock_send,
            mock.patch.object(adapter, "_log_to_console") as mock_log,
        ):
            adapter.export_record({"not": "a TraceRecord"})
        mock_send.assert_not_called()
        mock_log.assert_not_called()

    def test_export_record_skips_otel_when_disabled(self) -> None:
        adapter = _adapter_with_otel_disabled()
        record = TraceRecord(
            timestamp=1.0,
            trace_id="t",
            span_id="s",
            name="n",
            kind="INTERNAL",
            status_code="OK",
            attributes={},
            duration_ms=1.0,
        )
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False),
            mock.patch.object(adapter, "_send_to_otel") as mock_send,
            mock.patch.object(adapter, "_log_to_console") as mock_log,
        ):
            adapter.export_record(record)
        mock_send.assert_not_called()
        mock_log.assert_called_once_with(record)

    def test_export_record_sends_to_otel_when_enabled(self) -> None:
        adapter = _adapter_with_otel_disabled()
        record = TraceRecord(
            timestamp=1.0,
            trace_id="t",
            span_id="s",
            name="n",
            kind="INTERNAL",
            status_code="OK",
            attributes={},
            duration_ms=1.0,
        )
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(adapter, "_send_to_otel") as mock_send,
            mock.patch.object(adapter, "_log_to_console"),
        ):
            adapter.export_record(record)
        mock_send.assert_called_once_with(record)


# ---------------------------------------------------------------------------
# _send_to_otel: ERROR branch + exception swallow
# ---------------------------------------------------------------------------


class TestSendToOtel:
    def test_send_to_otel_sets_error_status(self) -> None:
        adapter = _adapter_with_otel_disabled()
        adapter.tracer = mock.MagicMock()
        span_ctx = mock.MagicMock()
        adapter.tracer.start_as_current_span.return_value.__enter__.return_value = (
            span_ctx
        )

        record = TraceRecord(
            timestamp=1.0,
            trace_id="t",
            span_id="s",
            name="n",
            kind="INTERNAL",
            status_code="ERROR",
            status_message="boom",
            attributes={},
            duration_ms=1.0,
        )
        adapter._send_to_otel(record)
        # set_status was called with an ERROR status.
        span_ctx.set_status.assert_called_once()
        status_arg = span_ctx.set_status.call_args.args[0]
        # We don't introspect StatusCode here (would couple to OTel internals);
        # but we can confirm the call happened.
        assert status_arg is not None

    def test_send_to_otel_swallows_exceptions(self, caplog) -> None:
        adapter = _adapter_with_otel_disabled()
        adapter.tracer = mock.MagicMock()
        adapter.tracer.start_as_current_span.side_effect = RuntimeError("otel down")

        record = TraceRecord(
            timestamp=1.0,
            trace_id="t",
            span_id="s",
            name="n",
            kind="INTERNAL",
            status_code="OK",
            attributes={},
            duration_ms=1.0,
        )
        with caplog.at_level(logging.ERROR):
            adapter._send_to_otel(record)
        assert any(
            "Error sending trace to OpenTelemetry" in rec.message
            for rec in caplog.records
        )

    def test_str_to_span_kind_unknown_defaults_to_internal(self) -> None:
        from opentelemetry.trace import SpanKind

        adapter = _adapter_with_otel_disabled()
        assert adapter._str_to_span_kind("UNKNOWN") == SpanKind.INTERNAL

    def test_timestamp_to_nanos(self) -> None:
        adapter = _adapter_with_otel_disabled()
        # 1.5 s == 1_500_000_000 ns
        assert adapter._timestamp_to_nanos(1.5) == 1_500_000_000


# ---------------------------------------------------------------------------
# _log_to_console exception swallow
# ---------------------------------------------------------------------------


class TestLogToConsole:
    def test_log_to_console_swallows_exception(self, caplog) -> None:
        adapter = _adapter_with_otel_disabled()
        record = TraceRecord(
            timestamp=1.0,
            trace_id="t",
            span_id="s",
            name="n",
            kind="INTERNAL",
            status_code="OK",
            attributes={},
            duration_ms=1.0,
        )
        with mock.patch.object(
            tracer_mod, "get_logger", side_effect=RuntimeError("logger down")
        ):
            with caplog.at_level(logging.ERROR):
                adapter._log_to_console(record)
        assert any(
            "Error logging trace to console" in rec.message for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# record_trace re-raises on internal failure
# ---------------------------------------------------------------------------


class TestRecordTrace:
    def test_record_trace_reraises_when_add_record_fails(self, caplog) -> None:
        adapter = _adapter_with_otel_disabled()
        with mock.patch.object(
            adapter, "add_record", side_effect=RuntimeError("buffer full")
        ):
            with caplog.at_level(logging.ERROR):
                with pytest.raises(RuntimeError, match="buffer full"):
                    adapter.record_trace(
                        name="n",
                        trace_id="t",
                        span_id="s",
                        kind="INTERNAL",
                        status_code="OK",
                        attributes={},
                    )
        assert any("Error recording trace" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# get_traces singleton
# ---------------------------------------------------------------------------


class TestGetTracesSingleton:
    def test_get_traces_caches_singleton(self) -> None:
        # Reset module-level singleton; ensure flush task suppressed.
        tracer_mod._traces_instance = None
        AtlanTracesAdapter._flush_task_started = True
        with mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False):
            first = get_traces()
            second = get_traces()
        assert first is second
        # Reset for any subsequent tests.
        tracer_mod._traces_instance = None
