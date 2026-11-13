"""Unit tests for application_sdk.observability.traces_adaptor.

Targets the uncovered branches:
- the adapter keeps nothing for later: no flush task, no buffer, no upload.
- setup_tracer_provider: off, OTLP-only default, dev-only console exporter,
  idempotence, reuse of a pre-installed global, install races, concurrent
  callers, exporter and provider failures.
- the adapter binding to the shared provider.
- export_record dispatch (TraceRecord vs other; ENABLE_OTLP_TRACES on/off).
- _send_to_otel span timing and parentage, ERROR-status branch and exception
  swallow.
- _log_to_console exception swallow.
- record_trace re-raises after logging.

OTel's global provider slot is faked (``otel_global``) so no test installs a
process-wide provider; the exporter-wiring tests build a real ``TracerProvider`` so they
check the processors actually attached.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from unittest import mock

import pytest
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import ProxyTracerProvider, SpanKind, StatusCode

from application_sdk.observability import traces_adaptor as tracer_mod
from application_sdk.observability.models import TraceRecord
from application_sdk.observability.traces_adaptor import AtlanTracesAdapter, get_traces


def _adapter_with_otel_disabled() -> AtlanTracesAdapter:
    """Build an adapter with OTLP disabled."""
    with mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False):
        return AtlanTracesAdapter()


# ---------------------------------------------------------------------------
# Nothing is kept for later: no flush task, no buffer, no upload
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> TraceRecord:
    fields: dict[str, object] = {
        "timestamp": 1.0,
        "trace_id": "t",
        "span_id": "s",
        "name": "n",
        "kind": "INTERNAL",
        "status_code": "OK",
        "attributes": {},
        "duration_ms": 1.0,
    }
    fields.update(overrides)
    return TraceRecord(**fields)  # type: ignore[arg-type]  # test-only kwargs bag


#: The switch every sink shares; the unit-test conftest turns it off.
_STORE_SINK_SWITCH = (
    "application_sdk.observability.observability.ENABLE_OBSERVABILITY_STORE_SINK"
)


class TestNothingKeptForLater:
    """FND-4913: the adapter used to buffer records, upload them as NDJSON
    under ``observability/.../traces/`` and start a periodic flush task on the
    loop that built it. Nothing read those files; the adapter now only emits."""

    def test_construction_starts_no_flush_task(self) -> None:
        # The once-per-process guard the old flush kept, cleared so a flush
        # would start here if construction still started one.
        with (
            mock.patch.object(
                AtlanTracesAdapter, "_flush_task_started", False, create=True
            ),
            mock.patch(
                "application_sdk.observability.observability.observability_loop"
            ) as shared_loop,
        ):
            _adapter_with_otel_disabled()
        shared_loop.start.assert_not_called()

    def test_record_trace_buffers_nothing_and_exports_at_once(self) -> None:
        adapter = _adapter_with_otel_disabled()
        with mock.patch.object(adapter, "_log_to_console") as mock_log:
            adapter.record_trace(
                name="n",
                trace_id="t",
                span_id="s",
                kind="INTERNAL",
                status_code="OK",
                attributes={},
            )
        mock_log.assert_called_once()
        assert adapter._buffer == []
        assert adapter._flush_tasks == set()

    def test_store_sink_is_off_even_when_the_shared_switch_is_on(self) -> None:
        with mock.patch(_STORE_SINK_SWITCH, True):
            assert _adapter_with_otel_disabled()._store_sink_enabled() is False

    async def test_flush_uploads_nothing(self) -> None:
        adapter = _adapter_with_otel_disabled()
        adapter._buffer.append(adapter.process_record(_record()))
        with (
            mock.patch(_STORE_SINK_SWITCH, True),
            mock.patch(
                "application_sdk.observability.observability._upload_to_observability_stores"
            ) as upload,
        ):
            await adapter._flush_buffer(force=True)
        upload.assert_not_called()


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


@pytest.fixture
def recorded_spans() -> "Iterator[tuple[AtlanTracesAdapter, InMemorySpanExporter]]":
    """An adapter whose tracer exports to memory, on a provider of its own."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    adapter = _adapter_with_otel_disabled()
    adapter.tracer = provider.get_tracer("test")
    yield adapter, exporter
    provider.shutdown()


class TestSendToOtel:
    def test_span_covers_the_recorded_duration(self, recorded_spans) -> None:
        """FND-4913: the span used to open at export time and close at once,
        so every span read 0 ms whatever the call took."""
        adapter, exporter = recorded_spans
        adapter._send_to_otel(_record(timestamp=100.0, duration_ms=250.0))
        (span,) = exporter.get_finished_spans()
        assert span.end_time == 100_000_000_000
        assert span.end_time - span.start_time == 250_000_000

    def test_span_is_a_child_of_the_current_span(self, recorded_spans) -> None:
        adapter, exporter = recorded_spans
        with adapter.tracer.start_as_current_span("caller") as caller:
            adapter._send_to_otel(_record(timestamp=100.0))
        recorded = next(s for s in exporter.get_finished_spans() if s.name == "n")
        assert recorded.parent is not None
        assert recorded.parent.span_id == caller.get_span_context().span_id

    def test_span_carries_kind_attributes_and_events(self, recorded_spans) -> None:
        adapter, exporter = recorded_spans
        adapter._send_to_otel(
            _record(
                kind="SERVER",
                attributes={"function": "f"},
                events=[{"name": "e", "timestamp": 0.5, "attributes": {"k": "v"}}],
            )
        )
        (span,) = exporter.get_finished_spans()
        assert span.kind == SpanKind.SERVER
        assert dict(span.attributes or {}) == {"function": "f"}
        (event,) = span.events
        assert (event.name, event.timestamp, dict(event.attributes or {})) == (
            "e",
            500_000_000,
            {"k": "v"},
        )

    def test_send_to_otel_sets_error_status(self, recorded_spans) -> None:
        adapter, exporter = recorded_spans
        adapter._send_to_otel(_record(status_code="ERROR", status_message="boom"))
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "boom"

    def test_send_to_otel_swallows_exceptions(self, caplog) -> None:
        adapter = _adapter_with_otel_disabled()
        adapter.tracer = mock.MagicMock()
        adapter.tracer.start_span.side_effect = RuntimeError("otel down")

        with caplog.at_level(logging.ERROR):
            adapter._send_to_otel(_record())
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
        tracer_mod._traces_instance = None
        with mock.patch.object(tracer_mod, "ENABLE_OTLP_TRACES", False):
            first = get_traces()
            second = get_traces()
        assert first is second
        # Reset for any subsequent tests.
        tracer_mod._traces_instance = None
