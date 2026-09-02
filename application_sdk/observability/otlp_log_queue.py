"""Severity-routed OTLP log export with drop accounting.

Why this exists
---------------
The OpenTelemetry ``BatchLogRecordProcessor`` buffers records in one bounded
deque per exporter. When the app emits faster than the collector accepts, the
deque evicts the *oldest* record regardless of severity and logs
``"Queue full, dropping Log."`` at most once per 20 seconds, with no count.

A production RCA showed the consequence: a publish phase logging two INFO
lines per asset produced ~7,500 records in 20 seconds, the queue overflowed,
and WARNING/ERROR records still waiting to be exported were evicted by INFO
chatter. The customer-facing run page then showed a clean run, because the only
survivor was the rate-limited "Queue full" line.

What this module does
---------------------
:class:`SeverityRoutedLogRecordProcessor` sits in front of *two* standard
``BatchLogRecordProcessor`` instances that share one endpoint:

* **priority lane** — records at or above :data:`PRIORITY_MIN_SEVERITY`
  (WARNING+). An INFO/DEBUG burst can never evict anything here, because it
  never enters this queue.
* **bulk lane** — everything else. It keeps the upstream oldest-evicts
  behaviour and is the lane that absorbs bursts.

Both lanes are the same size (``max_queue_size``), so WARNING+ capacity is a
full, dedicated queue rather than whatever INFO left over.

Every eviction is also *counted*, per lane, and reported through the SDK
logger as a WARNING with the numbers ("dropped N records: INFO/DEBUG=a,
WARNING+=b") — immediately on the first drop, then at most once per
:data:`DROP_REPORT_INTERVAL_SECONDS`, and once more at shutdown. The report
rides the priority lane, so a run that lost records is never indistinguishable
from a clean run on the consumer side.

Fullness detection reads two private attributes of the upstream processor
(``_batch_processor._queue`` / ``_max_queue_size``). If a future OTel release
renames them the routing still works and only the drop counters degrade to
silence — export itself never depends on them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk._logs._internal.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
)

_logger = logging.getLogger(__name__)

#: Records at or above this severity ride the priority lane.
PRIORITY_MIN_SEVERITY: SeverityNumber = SeverityNumber.WARN

#: Minimum seconds between two drop reports from one processor. The first
#: drop is reported immediately; later drops accumulate until this much time
#: has passed, then the next emission flushes the summary.
DROP_REPORT_INTERVAL_SECONDS: float = 30.0

# stdlib ``levelno`` -> OTel ``SeverityNumber`` for the five standard levels.
# The SDK's loguru levels map onto stdlib numbers (see ``SEVERITY_MAPPING``),
# so this is the only translation the OTLP path needs.
_LEVELNO_TO_SEVERITY: dict[int, SeverityNumber] = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}


def severity_from_levelno(levelno: int) -> SeverityNumber:
    """Map a stdlib log level number to the OTel ``SeverityNumber``.

    Exact standard levels map directly; a custom level between two standard
    ones takes the severity of the standard level just below it, matching how
    ``logging`` itself treats such levels. Anything below ``DEBUG`` (including
    ``NOTSET``) is ``UNSPECIFIED``.
    """
    if levelno in _LEVELNO_TO_SEVERITY:
        return _LEVELNO_TO_SEVERITY[levelno]
    for std_level in (
        logging.CRITICAL,
        logging.ERROR,
        logging.WARNING,
        logging.INFO,
        logging.DEBUG,
    ):
        if levelno >= std_level:
            return _LEVELNO_TO_SEVERITY[std_level]
    return SeverityNumber.UNSPECIFIED


def severity_value(record: ReadWriteLogRecord) -> int:
    """Return the record's severity as a comparable integer.

    Tolerates the three shapes seen in practice: a ``SeverityNumber`` enum, a
    bare stdlib ``levelno`` int (what older SDK builds stamped), or ``None``.
    """
    severity = getattr(getattr(record, "log_record", None), "severity_number", None)
    if isinstance(severity, SeverityNumber):
        return severity.value
    if isinstance(severity, int):
        return severity_from_levelno(severity).value
    return SeverityNumber.UNSPECIFIED.value


def _queue_is_full(processor: BatchLogRecordProcessor) -> bool:
    """True when the next ``on_emit`` on *processor* will evict a record.

    Reads upstream private state defensively: any shape mismatch means
    "unknown", reported as ``False`` so accounting degrades rather than
    export breaking.
    """
    batch_processor = getattr(processor, "_batch_processor", None)
    queue = getattr(batch_processor, "_queue", None)
    capacity = getattr(batch_processor, "_max_queue_size", None)
    if queue is None or not isinstance(capacity, int):
        return False
    try:
        return len(queue) >= capacity
    except TypeError:
        return False


class SeverityRoutedLogRecordProcessor(LogRecordProcessor):
    """Route WARNING+ records to a dedicated batch queue and count every drop.

    Args:
        exporter_factory: Builds one exporter per lane. Called exactly twice.
            Two exporters (two gRPC channels) keep the lanes independent — a
            stalled bulk export never blocks a priority flush.
        endpoint: Human-readable destination, used only in the drop report.
        schedule_delay_millis: Passed through to both lanes.
        max_export_batch_size: Passed through to both lanes.
        max_queue_size: Per-lane queue capacity. Total buffered records can
            reach ``2 * max_queue_size``.
        priority_min_severity: Lowest severity that rides the priority lane.
        drop_report_interval_seconds: Rate limit for drop reports.
        clock: Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        exporter_factory: Callable[[], LogRecordExporter],
        *,
        endpoint: str,
        schedule_delay_millis: float,
        max_export_batch_size: int,
        max_queue_size: int,
        priority_min_severity: SeverityNumber = PRIORITY_MIN_SEVERITY,
        drop_report_interval_seconds: float = DROP_REPORT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._endpoint = endpoint
        self._priority_min = priority_min_severity.value
        self._report_interval = drop_report_interval_seconds
        self._clock = clock

        def _lane() -> BatchLogRecordProcessor:
            return BatchLogRecordProcessor(
                exporter_factory(),
                schedule_delay_millis=schedule_delay_millis,
                max_export_batch_size=max_export_batch_size,
                max_queue_size=max_queue_size,
            )

        self._bulk = _lane()
        self._priority = _lane()

        self._lock = threading.Lock()
        self._pending_bulk = 0
        self._pending_priority = 0
        self._total_bulk = 0
        self._total_priority = 0
        self._last_report: float | None = None
        self._reporting = threading.local()

    # ------------------------------------------------------------------ routing

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        is_priority = severity_value(log_record) >= self._priority_min
        lane = self._priority if is_priority else self._bulk
        if _queue_is_full(lane):
            with self._lock:
                if is_priority:
                    self._pending_priority += 1
                    self._total_priority += 1
                else:
                    self._pending_bulk += 1
                    self._total_bulk += 1
        lane.on_emit(log_record)
        self._maybe_report()

    def shutdown(self) -> None:
        self._maybe_report(force=True)
        self._priority.shutdown()
        self._bulk.shutdown()

    def force_flush(self, timeout_millis: int | None = None) -> bool:
        flushed_priority = self._priority.force_flush(timeout_millis)
        flushed_bulk = self._bulk.force_flush(timeout_millis)
        return bool(flushed_priority and flushed_bulk)

    # --------------------------------------------------------------- reporting

    @property
    def dropped_counts(self) -> dict[str, int]:
        """Lifetime drop counters, keyed ``bulk`` / ``priority``."""
        with self._lock:
            return {"bulk": self._total_bulk, "priority": self._total_priority}

    def _maybe_report(self, *, force: bool = False) -> None:
        # The report is itself a log record that re-enters ``on_emit`` through
        # the SDK's stdlib bridge; the thread-local guard keeps that single
        # level of recursion from producing a second report.
        if getattr(self._reporting, "active", False):
            return
        with self._lock:
            pending = self._pending_bulk + self._pending_priority
            if pending == 0:
                return
            now = self._clock()
            if (
                not force
                and self._last_report is not None
                and now - self._last_report < self._report_interval
            ):
                return
            bulk, priority = self._pending_bulk, self._pending_priority
            self._pending_bulk = self._pending_priority = 0
            since = None if self._last_report is None else now - self._last_report
            self._last_report = now

        self._reporting.active = True
        try:
            window = (
                "since the previous report"
                if since is None
                else f"in the last {since:.0f}s"
            )
            _logger.warning(
                "OTLP log export dropped %d record(s) %s (INFO/DEBUG=%d, WARNING+=%d): "
                "the collector at %s accepted records slower than the app emitted them. "
                "WARNING and above use a separate queue, so they are only lost when "
                "the WARNING+ count is non-zero.",
                bulk + priority,
                window,
                bulk,
                priority,
                self._endpoint,
            )
        finally:
            self._reporting.active = False


__all__: list[Any] = [
    "DROP_REPORT_INTERVAL_SECONDS",
    "PRIORITY_MIN_SEVERITY",
    "SeverityRoutedLogRecordProcessor",
    "severity_from_levelno",
    "severity_value",
]
