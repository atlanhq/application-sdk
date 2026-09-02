"""Tests for the severity-routed OTLP log processor.

Two layers:

* Fake lanes (``BatchLogRecordProcessor`` patched) pin the routing, drop
  accounting, rate limiting, re-entrancy guard and shutdown/flush forwarding
  without starting export threads.
* One real-lane test proves the guarantee end to end: with an exporter that
  blocks, an INFO flood overflows the bulk lane while the WARNING record stays
  queued and is exported once the collector recovers.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import ReadWriteLogRecord

from application_sdk.observability import otlp_log_queue
from application_sdk.observability.otlp_log_queue import (
    SeverityRoutedLogRecordProcessor,
    severity_from_levelno,
    severity_value,
)

MODULE_LOGGER = "application_sdk.observability.otlp_log_queue"
ENDPOINT = "http://collector.example:4317"


def _record(severity: Any, body: str = "x") -> ReadWriteLogRecord:
    return ReadWriteLogRecord(log_record=LogRecord(severity_number=severity, body=body))


# ---------------------------------------------------------------------------
# severity helpers
# ---------------------------------------------------------------------------


class TestSeverityHelpers:
    @pytest.mark.parametrize(
        ("levelno", "expected"),
        [
            (logging.DEBUG, SeverityNumber.DEBUG),
            (logging.INFO, SeverityNumber.INFO),
            (logging.WARNING, SeverityNumber.WARN),
            (logging.ERROR, SeverityNumber.ERROR),
            (logging.CRITICAL, SeverityNumber.FATAL),
            # custom levels take the standard level just below them
            (25, SeverityNumber.INFO),
            (35, SeverityNumber.WARN),
            (60, SeverityNumber.FATAL),
            # below DEBUG (incl. NOTSET) is unspecified
            (logging.NOTSET, SeverityNumber.UNSPECIFIED),
            (5, SeverityNumber.UNSPECIFIED),
        ],
    )
    def test_severity_from_levelno(self, levelno: int, expected: SeverityNumber):
        assert severity_from_levelno(levelno) is expected

    def test_severity_value_accepts_enum_int_and_none(self):
        assert (
            severity_value(_record(SeverityNumber.ERROR)) == SeverityNumber.ERROR.value
        )
        # A bare stdlib levelno (what older SDK builds stamped) is translated.
        assert severity_value(_record(logging.WARNING)) == SeverityNumber.WARN.value
        assert severity_value(_record(None)) == SeverityNumber.UNSPECIFIED.value
        # Records lacking the attribute path degrade to unspecified, never raise.
        assert severity_value(SimpleNamespace()) == SeverityNumber.UNSPECIFIED.value  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fake lanes: routing + accounting
# ---------------------------------------------------------------------------


class _FakeLane:
    """Stand-in for BatchLogRecordProcessor exposing the same private shape."""

    def __init__(
        self,
        exporter: Any,
        *,
        schedule_delay_millis: float,
        max_export_batch_size: int,
        max_queue_size: int,
    ) -> None:
        self.exporter = exporter
        self.kwargs = {
            "schedule_delay_millis": schedule_delay_millis,
            "max_export_batch_size": max_export_batch_size,
            "max_queue_size": max_queue_size,
        }
        self._batch_processor = SimpleNamespace(
            _queue=deque(maxlen=max_queue_size), _max_queue_size=max_queue_size
        )
        self.emitted: list[ReadWriteLogRecord] = []
        self.shutdown_calls = 0
        self.flush_calls: list[int | None] = []
        self.flush_result = True

    def on_emit(self, record: ReadWriteLogRecord) -> None:
        # Same semantics as upstream: appendleft on a bounded deque evicts the oldest.
        self._batch_processor._queue.appendleft(record)
        self.emitted.append(record)

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def force_flush(self, timeout_millis: int | None = None) -> bool:
        self.flush_calls.append(timeout_millis)
        return self.flush_result


@pytest.fixture
def clock() -> list[float]:
    return [1000.0]


@pytest.fixture
def router(clock: list[float]):
    factory = mock.Mock(side_effect=lambda: mock.Mock(name="exporter"))
    with mock.patch.object(otlp_log_queue, "BatchLogRecordProcessor", _FakeLane):
        proc = SeverityRoutedLogRecordProcessor(
            factory,
            endpoint=ENDPOINT,
            schedule_delay_millis=5000,
            max_export_batch_size=2,
            max_queue_size=3,
            drop_report_interval_seconds=30.0,
            clock=lambda: clock[0],
        )
    proc.exporter_factory = factory  # type: ignore[attr-defined]
    return proc


class TestRouting:
    def test_builds_two_lanes_with_own_exporters(self, router):
        assert router.exporter_factory.call_count == 2
        assert router._bulk.exporter is not router._priority.exporter
        for lane in (router._bulk, router._priority):
            assert lane.kwargs == {
                "schedule_delay_millis": 5000,
                "max_export_batch_size": 2,
                "max_queue_size": 3,
            }

    @pytest.mark.parametrize(
        "severity",
        [
            SeverityNumber.WARN,
            SeverityNumber.ERROR,
            SeverityNumber.FATAL,
            logging.WARNING,
            45,
        ],
    )
    def test_warning_and_above_ride_priority_lane(self, router, severity):
        rec = _record(severity)
        router.on_emit(rec)
        assert router._priority.emitted == [rec]
        assert router._bulk.emitted == []

    @pytest.mark.parametrize(
        "severity", [SeverityNumber.INFO, SeverityNumber.DEBUG, logging.INFO, None]
    )
    def test_info_and_below_ride_bulk_lane(self, router, severity):
        rec = _record(severity)
        router.on_emit(rec)
        assert router._bulk.emitted == [rec]
        assert router._priority.emitted == []

    def test_info_flood_never_evicts_a_warning(self, router):
        warn = _record(SeverityNumber.WARN, "the one that matters")
        router.on_emit(warn)
        for i in range(50):
            router.on_emit(_record(SeverityNumber.INFO, f"info-{i}"))
        assert list(router._priority._batch_processor._queue) == [warn]
        assert len(router._bulk._batch_processor._queue) == 3  # capacity, not 50


class TestDropAccounting:
    def _flood_bulk(self, router, n: int) -> None:
        for i in range(n):
            router.on_emit(_record(SeverityNumber.INFO, f"info-{i}"))

    def test_first_drop_reported_immediately_with_counts(self, router, caplog):
        caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
        self._flood_bulk(router, 3)  # fills to capacity, nothing dropped yet
        assert router.dropped_counts == {"bulk": 0, "priority": 0}
        assert caplog.records == []

        self._flood_bulk(router, 1)  # 4th record evicts the oldest
        assert router.dropped_counts == {"bulk": 1, "priority": 0}
        reports = [r for r in caplog.records if r.name == MODULE_LOGGER]
        assert len(reports) == 1
        msg = reports[0].getMessage()
        assert reports[0].levelno == logging.WARNING
        assert "dropped 1 record(s)" in msg
        assert "INFO/DEBUG=1, WARNING+=0" in msg
        assert ENDPOINT in msg

    def test_reports_are_rate_limited_then_flushed_after_interval(
        self, router, clock, caplog
    ):
        caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
        self._flood_bulk(router, 4)  # first drop -> immediate report
        self._flood_bulk(router, 10)  # 10 more drops inside the interval
        reports = [r for r in caplog.records if r.name == MODULE_LOGGER]
        assert len(reports) == 1, "second report must wait for the interval"

        clock[0] += 31.0
        router.on_emit(
            _record(SeverityNumber.INFO, "tick")
        )  # 11th drop, interval elapsed
        reports = [r for r in caplog.records if r.name == MODULE_LOGGER]
        assert len(reports) == 2
        assert "dropped 11 record(s) in the last 31s" in reports[1].getMessage()
        assert router.dropped_counts == {"bulk": 12, "priority": 0}

    def test_priority_drops_are_counted_separately(self, router, caplog):
        caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
        for i in range(4):
            router.on_emit(_record(SeverityNumber.ERROR, f"err-{i}"))
        assert router.dropped_counts == {"bulk": 0, "priority": 1}
        report = next(r for r in caplog.records if r.name == MODULE_LOGGER)
        assert "INFO/DEBUG=0, WARNING+=1" in report.getMessage()

    def test_shutdown_flushes_pending_report_and_shuts_both_lanes(self, router, caplog):
        caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
        self._flood_bulk(router, 4)  # immediate report for drop #1
        self._flood_bulk(router, 5)  # 5 pending, inside the interval
        router.shutdown()
        reports = [r for r in caplog.records if r.name == MODULE_LOGGER]
        assert len(reports) == 2
        assert "dropped 5 record(s)" in reports[1].getMessage()
        assert router._bulk.shutdown_calls == 1
        assert router._priority.shutdown_calls == 1

    def test_shutdown_without_drops_emits_no_report(self, router, caplog):
        caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
        router.on_emit(_record(SeverityNumber.INFO))
        router.shutdown()
        assert [r for r in caplog.records if r.name == MODULE_LOGGER] == []

    def test_force_flush_forwards_to_both_lanes(self, router):
        assert router.force_flush(timeout_millis=1234) is True
        assert router._bulk.flush_calls == [1234]
        assert router._priority.flush_calls == [1234]
        router._bulk.flush_result = False
        assert router.force_flush() is False

    def test_report_reentry_does_not_recurse_or_double_report(self, router, caplog):
        """The report re-enters on_emit through the SDK's stdlib bridge in prod.

        Simulate that bridge with a handler that feeds the report back in as a
        WARNING record: exactly one report must be produced, with no recursion.
        """
        caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
        module_logger = logging.getLogger(MODULE_LOGGER)

        class _Bridge(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                router.on_emit(_record(SeverityNumber.WARN, record.getMessage()))

        bridge = _Bridge()
        module_logger.addHandler(bridge)
        try:
            self._flood_bulk(router, 4)
        finally:
            module_logger.removeHandler(bridge)

        reports = [r for r in caplog.records if r.name == MODULE_LOGGER]
        assert len(reports) == 1
        # The bridged report landed on the priority lane like any WARNING.
        assert [r.log_record.body for r in router._priority.emitted] == [
            reports[0].getMessage()
        ]

    def test_accounting_degrades_silently_when_private_shape_changes(self, router):
        # Simulate an upstream OTel release renaming the private queue attrs:
        # the lane still accepts records, but exposes no _queue/_max_queue_size.
        router._bulk._batch_processor = SimpleNamespace()
        router._bulk.on_emit = router._bulk.emitted.append
        for i in range(10):
            router.on_emit(_record(SeverityNumber.INFO, f"info-{i}"))
        assert len(router._bulk.emitted) == 10  # export path unaffected
        assert router.dropped_counts == {"bulk": 0, "priority": 0}


# ---------------------------------------------------------------------------
# real lanes: end-to-end guarantee with a stalled exporter
# ---------------------------------------------------------------------------


class _BlockingExporter:
    """Exporter whose first export blocks until released, mimicking a stalled collector."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exported: list[str] = []
        self._lock = threading.Lock()

    def export(self, batch):  # noqa: ANN001 - upstream protocol
        self.entered.set()
        self.release.wait(timeout=10)
        with self._lock:
            self.exported.extend(str(r.log_record.body) for r in batch)
        return 0

    def shutdown(self, **kwargs: Any) -> None:
        self.release.set()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@pytest.mark.timeout(30)
def test_real_lanes_warning_survives_info_flood_while_collector_stalls():
    exporters: list[_BlockingExporter] = []

    def factory() -> _BlockingExporter:
        exporters.append(_BlockingExporter())
        return exporters[-1]

    capacity = 8
    router = SeverityRoutedLogRecordProcessor(
        factory,
        endpoint=ENDPOINT,
        schedule_delay_millis=60_000,  # only batch-size wakeups during the test
        max_export_batch_size=capacity,
        max_queue_size=capacity,
    )
    bulk_exporter, priority_exporter = exporters
    try:
        # First batch fills the bulk lane; the worker takes it and blocks in export.
        for i in range(capacity):
            router.on_emit(_record(SeverityNumber.INFO, f"batch1-{i}"))
        assert bulk_exporter.entered.wait(timeout=10), "worker never started exporting"

        # While the collector stalls: a warning arrives, then INFO keeps flooding.
        router.on_emit(_record(SeverityNumber.WARN, "permission denied"))
        for i in range(capacity + 5):
            router.on_emit(_record(SeverityNumber.INFO, f"flood-{i}"))

        counts = router.dropped_counts
        assert counts["bulk"] == 5, counts
        assert counts["priority"] == 0, counts
        assert [
            str(r.log_record.body) for r in router._priority._batch_processor._queue
        ] == ["permission denied"]
    finally:
        bulk_exporter.release.set()
        priority_exporter.release.set()
        router.shutdown()

    # Collector recovered: the warning was exported, the flood lost only its excess.
    assert priority_exporter.exported == ["permission denied"]
    assert "batch1-0" in bulk_exporter.exported
    assert len(bulk_exporter.exported) == capacity * 2  # two full batches survived
