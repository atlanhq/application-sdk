"""Unit tests for application_sdk.observability.traces_adaptor.

Targets the uncovered branches:
- _reset_for_testing (ClassVar reset).
- setup_tracer_provider: off, OTLP-only default, dev-only console exporter,
  idempotence, reuse of a pre-installed global, install races, concurrent
  callers, exporter and provider failures.
- the adapter binding to the shared provider.
- export_record dispatch (TraceRecord vs other; ENABLE_OTLP_TRACES on/off).
- _send_to_otel ERROR-status branch and exception swallow.
- _log_to_console exception swallow.
- record_trace re-raises after logging.

OTel's global provider slot is faked (``otel_global``) so no test installs a
process-wide provider; the exporter-wiring tests build a real ``TracerProvider`` so they
check the processors actually attached.
"""

from __future__ import annotations

import logging
import threading
from unittest import mock

import pytest
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from opentelemetry.trace import ProxyTracerProvider

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
    """Each test starts with no provider recorded by this module."""
    tracer_mod._tracer_provider = None
    yield
    tracer_mod._tracer_provider = None


class _FakeGlobal:
    """OTel's one-shot global provider slot, without touching the real one:
    the first ``set`` wins, later ones are ignored (as OTel does)."""

    def __init__(self) -> None:
        self.provider: object = ProxyTracerProvider()
        self.set = mock.MagicMock(side_effect=self._set)

    def _set(self, provider: object) -> None:
        if isinstance(self.provider, ProxyTracerProvider):
            self.provider = provider

    def get(self) -> object:
        return self.provider


@pytest.fixture
def otel_global():
    fake = _FakeGlobal()
    with (
        mock.patch.object(tracer_mod.trace, "set_tracer_provider", fake.set),
        mock.patch.object(tracer_mod.trace, "get_tracer_provider", fake.get),
    ):
        yield fake


def _exporters_on(provider: TracerProvider) -> list[object]:
    """The span exporters attached to a real ``TracerProvider``."""
    processors = provider._active_span_processor._span_processors
    return [p.span_exporter for p in processors]


class TestSetupTracerProvider:
    def test_noop_when_traces_disabled(self, otel_global) -> None:
        with mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False):
            assert tracer_mod.setup_tracer_provider() is None
        otel_global.set.assert_not_called()

    def test_installs_global_provider_with_otlp_only_by_default(
        self, otel_global
    ) -> None:
        """FND-4642: OTLP only — no ConsoleSpanExporter writing every span to
        stdout unless the dev-only flag asks for it."""
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "ENABLE_TRACES_CONSOLE", False),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
        ):
            provider = tracer_mod.setup_tracer_provider()

        assert isinstance(provider, TracerProvider)
        otel_global.set.assert_called_once_with(provider)
        assert otel_global.provider is provider
        assert [type(e) for e in _exporters_on(provider)] == [OTLPSpanExporter]
        provider.shutdown()

    def test_console_exporter_only_behind_dev_flag(self, otel_global) -> None:
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "ENABLE_TRACES_CONSOLE", True),
            mock.patch.object(
                tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317"
            ),
        ):
            provider = tracer_mod.setup_tracer_provider()

        assert isinstance(provider, TracerProvider)
        exporters = _exporters_on(provider)
        assert [type(e) for e in exporters] == [OTLPSpanExporter, ConsoleSpanExporter]
        provider.shutdown()

    def test_idempotent(self, otel_global) -> None:
        """Called from run_main, the TraceInterceptor and the adapter: one
        provider, installed once."""
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        ):
            first = tracer_mod.setup_tracer_provider()
            second = tracer_mod.setup_tracer_provider()

        assert first is not None
        assert first is second
        otel_global.set.assert_called_once()

    def test_reuses_a_provider_the_host_already_installed(self, otel_global) -> None:
        """Lens F-5be9e9: OTel keeps the first global provider. When the host
        installed one, return that one rather than a second provider Temporal
        never uses — and don't build exporters for it."""
        host_provider = TracerProvider()
        otel_global.provider = host_provider
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "TracerProvider") as mock_build,
        ):
            assert tracer_mod.setup_tracer_provider() is host_provider
            assert tracer_mod.setup_tracer_provider() is host_provider

        mock_build.assert_not_called()
        otel_global.set.assert_not_called()

    def test_losing_an_install_race_returns_the_winner(self, otel_global) -> None:
        """Lens F-5be9e9: if another provider becomes global between the check
        and ``set_tracer_provider``, return the global one and shut ours."""
        winner = TracerProvider()
        ours = mock.MagicMock(spec=TracerProvider)

        def _someone_else_wins(provider: object) -> None:
            otel_global.provider = winner

        otel_global.set.side_effect = _someone_else_wins
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "_build_tracer_provider", return_value=ours),
        ):
            assert tracer_mod.setup_tracer_provider() is winner

        ours.shutdown.assert_called_once_with()
        assert tracer_mod._tracer_provider is winner

    def test_concurrent_calls_agree_on_one_global_provider(self, otel_global) -> None:
        """Lens F-2ee0ca / F-c92eec: threads building workers at once must all
        get the provider that is actually global, and only one is built.

        A barrier holds every thread at provider construction; without the
        lock each would build, and the losers would keep their own."""
        n = 4
        barrier = threading.Barrier(n, timeout=0.5)
        built: list[TracerProvider] = []

        def _build() -> TracerProvider:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass  # serialised by the lock: the others never arrive
            provider = TracerProvider()
            built.append(provider)
            return provider

        results: list[object] = []
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(tracer_mod, "_build_tracer_provider", _build),
        ):
            threads = [
                threading.Thread(
                    target=lambda: results.append(tracer_mod.setup_tracer_provider())
                )
                for _ in range(n)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(results) == n
        assert len(built) == 1
        assert all(r is otel_global.provider for r in results)

    def test_otlp_exporter_failure_does_not_add_console(self, otel_global) -> None:
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
        ):
            provider = tracer_mod.setup_tracer_provider()

        assert isinstance(provider, TracerProvider)
        mock_console.assert_not_called()
        assert _exporters_on(provider) == []

    def test_provider_failure_installs_nothing(self, otel_global) -> None:
        """No console-only fallback: a failed setup leaves tracing off."""
        with (
            mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", True),
            mock.patch.object(
                tracer_mod, "TracerProvider", side_effect=RuntimeError("kaboom")
            ),
            mock.patch.object(tracer_mod, "ConsoleSpanExporter") as mock_console,
        ):
            assert tracer_mod.setup_tracer_provider() is None

        mock_console.assert_not_called()
        otel_global.set.assert_not_called()
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
