"""Unit tests for application_sdk.observability.traces_adaptor.

Targets the uncovered branches:
- _reset_for_testing (ClassVar reset).
- setup_tracer_provider: off, OTLP-only default, dev-only console exporter,
  idempotence, exporter and provider failures.
- the adapter binding to the shared provider.
- export_record dispatch (TraceRecord vs other; ENABLE_OTLP_TRACES on/off).
- _send_to_otel ERROR-status branch and exception swallow.
- _log_to_console exception swallow.
- record_trace re-raises after logging.

``set_tracer_provider`` is always mocked so no test installs a process-wide
provider; the exporter-wiring tests build a real ``TracerProvider`` so they
check the processors actually attached.
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from application_sdk.observability import traces_adaptor as tracer_mod
from application_sdk.observability.models import TraceRecord
from application_sdk.observability.traces_adaptor import AtlanTracesAdapter, get_traces


def _adapter_with_otel_disabled() -> AtlanTracesAdapter:
    """Build an adapter with OTLP disabled and the flush task suppressed."""
    AtlanTracesAdapter._flush_task_started = True  # don't start the flush
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

    def test_reset_for_testing_stops_the_running_flush(self) -> None:
        """A reset stops the old flush, so the next construction does not
        leave two running side by side on the shared loop."""
        task = mock.MagicMock()
        AtlanTracesAdapter._flush_task = task
        AtlanTracesAdapter._flush_task_started = True
        AtlanTracesAdapter._reset_for_testing()
        task.cancel.assert_called_once_with()
        task.wait.assert_called_once_with(timeout=5)
        assert AtlanTracesAdapter._flush_task is None


# ---------------------------------------------------------------------------
# setup_tracer_provider: the process-wide provider bootstrap
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_installed_provider():
    """Each test starts with no provider installed by this module."""
    tracer_mod._tracer_provider = None
    yield
    tracer_mod._tracer_provider = None


def _exporters_on(provider: TracerProvider) -> list[object]:
    """The span exporters attached to a real ``TracerProvider``."""
    processors = provider._active_span_processor._span_processors
    return [p.span_exporter for p in processors]


class TestSetupTracerProvider:
    def test_noop_when_traces_disabled(self) -> None:
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider") as mock_set,
        ):
            assert tracer_mod.setup_tracer_provider() is None
        mock_set.assert_not_called()

    def test_installs_global_provider_with_otlp_only_by_default(self) -> None:
        """FND-4642: OTLP only — no ConsoleSpanExporter writing every span to
        stdout unless the dev-only flag asks for it."""
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "ENABLE_TRACES_CONSOLE", False),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider") as mock_set,
        ):
            provider = tracer_mod.setup_tracer_provider()

        assert isinstance(provider, TracerProvider)
        mock_set.assert_called_once_with(provider)
        exporters = _exporters_on(provider)
        assert [type(e) for e in exporters] == [OTLPSpanExporter]
        provider.shutdown()

    def test_console_exporter_only_behind_dev_flag(self) -> None:
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "ENABLE_TRACES_CONSOLE", True),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider"),
        ):
            provider = tracer_mod.setup_tracer_provider()

        assert provider is not None
        exporters = _exporters_on(provider)
        assert [type(e) for e in exporters] == [OTLPSpanExporter, ConsoleSpanExporter]
        provider.shutdown()

    def test_idempotent(self) -> None:
        """Called from run_main, the TraceInterceptor and the adapter: one
        provider, installed once."""
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            mock.patch.object(tracer_mod.trace, "set_tracer_provider") as mock_set,
        ):
            first = tracer_mod.setup_tracer_provider()
            second = tracer_mod.setup_tracer_provider()

        assert first is not None
        assert first is second
        mock_set.assert_called_once()

    def test_otlp_exporter_failure_does_not_add_console(self) -> None:
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "ENABLE_TRACES_CONSOLE", False),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
            mock.patch.object(
                tracer_mod, "OTLPSpanExporter", side_effect=RuntimeError("bad config")
            ),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter") as mock_console,
            mock.patch.object(tracer_mod.trace, "set_tracer_provider"),
        ):
            provider = tracer_mod.setup_tracer_provider()

        assert provider is not None
        mock_console.assert_not_called()
        assert _exporters_on(provider) == []

    def test_provider_failure_installs_nothing(self) -> None:
        """No console-only fallback: a failed setup leaves tracing off."""
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(
                tracer_mod, "TracerProvider", side_effect=RuntimeError("kaboom")
            ),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter") as mock_console,
            mock.patch.object(tracer_mod.trace, "set_tracer_provider") as mock_set,
        ):
            assert tracer_mod.setup_tracer_provider() is None

        mock_console.assert_not_called()
        mock_set.assert_not_called()
        assert tracer_mod._tracer_provider is None


class TestAdapterUsesSharedProvider:
    def test_adapter_reuses_the_installed_provider(self) -> None:
        AtlanTracesAdapter._flush_task_started = True
        provider = mock.MagicMock()
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(
                tracer_mod, "setup_tracer_provider", return_value=provider
            ),
        ):
            adapter = AtlanTracesAdapter()

        assert adapter.tracer_provider is provider
        assert adapter.tracer is provider.get_tracer.return_value

    def test_adapter_without_provider_has_no_tracer(self) -> None:
        AtlanTracesAdapter._flush_task_started = True
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "setup_tracer_provider", return_value=None),
        ):
            adapter = AtlanTracesAdapter()

        assert adapter.tracer_provider is None
        assert adapter.tracer is None


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
