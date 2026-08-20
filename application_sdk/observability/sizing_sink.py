"""Durable sink for activity sizing observations.

The OTel histograms in ``sizing`` answer "is this tier wrong". Fitting a rule that
predicts a tier needs the rows themselves — a histogram bucket cannot give you
``peak_per_input_byte`` per execution — so each observation is also buffered and
uploaded as a record.

**Rides the existing observability pipeline rather than adding one.**
``AtlanObservability`` already does batching, hive partitioning by
year/month/day/hour, gzipped NDJSON, upload to the deployment store *and* the
upstream Atlan store when ``ENABLE_ATLAN_UPLOAD`` is set, and retention cleanup.
That upstream leg is also what makes cross-tenant collection work: records from
every tenant land under one prefix, already partitioned, with no new transport to
build or secure.

Every record carries ``schema_version``. The fields here will change — a driver
variable will be added, a basis renamed — and an analysis reading a mixed pile of
rows has to be able to tell which contract each one was written against rather
than inferring it from which keys happen to be present.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    SIZING_BATCH_SIZE,
    SIZING_CLEANUP_ENABLED,
    SIZING_FILE_NAME,
    SIZING_FLUSH_INTERVAL_SECONDS,
    SIZING_RETENTION_DAYS,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.utils import get_observability_dir

logger = get_logger(__name__)

#: Bump on any change to the record's fields or their meaning. Rows are read
#: months after they were written, mixed across SDK versions.
#: 2 — added started_at / pod / concurrency_max / is_attributable. A v1 row cannot
#: say whether its peak was pod-wide, so v1 and v2 must not be pooled.
SIZING_SCHEMA_VERSION = 2


class SizingObservabilitySink(AtlanObservability[Any]):
    """Buffers sizing observations and uploads them as gzipped NDJSON."""

    def process_record(self, record: Any) -> dict[str, Any]:
        """Flatten one observation into the row that gets written.

        ``app`` and ``deployment`` are stamped here rather than left to the
        partition path: once records from many tenants sit under one upstream
        prefix, a row that cannot say which tenant it came from cannot be used to
        fit that tenant's tiers — and cross-tenant rows must not be pooled blindly,
        since a tenant's data volume is the very thing being measured.
        """
        row: dict[str, Any] = {
            "schema_version": SIZING_SCHEMA_VERSION,
            "app": APPLICATION_NAME,
            "deployment": DEPLOYMENT_NAME,
        }
        row.update(asdict(record))
        # Derived here so every consumer computes them identically.
        row["mean_cpu_cores"] = record.mean_cpu_cores
        row["peak_per_input_byte"] = record.peak_per_input_byte
        # Written out rather than left for the reader to derive from
        # concurrency_max: it is the flag that decides which model a row feeds,
        # and a consumer that forgets to apply it pools two different quantities.
        row["is_attributable"] = record.is_attributable
        return row

    def export_record(self, record: Any) -> None:
        """No-op: the OTel histograms are emitted by ``sizing.record_observation``.

        Kept separate on purpose. Metrics are for watching, these rows are for
        fitting, and emitting the histograms from here would tie a live dashboard's
        correctness to a batching sink's flush schedule.
        """


_sink: SizingObservabilitySink | None = None
_sink_lock = threading.Lock()


def get_sink() -> SizingObservabilitySink | None:
    """The process-wide sink, created on first use, or ``None`` if unavailable.

    Lazy because constructing it touches the filesystem and registers a flush
    task; a worker with collection disabled should pay neither.
    """
    global _sink
    if _sink is not None:
        return _sink
    with _sink_lock:
        if _sink is None:
            try:
                _sink = SizingObservabilitySink(
                    batch_size=SIZING_BATCH_SIZE,
                    flush_interval=SIZING_FLUSH_INTERVAL_SECONDS,
                    retention_days=SIZING_RETENTION_DAYS,
                    cleanup_enabled=SIZING_CLEANUP_ENABLED,
                    data_dir=get_observability_dir(),
                    file_name=SIZING_FILE_NAME,
                )
            # conformance: ignore[E004] telemetry; a sink that cannot be built must not stop collection
            except Exception:
                logger.debug("sizing sink unavailable", exc_info=True)
                return None
    return _sink


def persist(observation: Any) -> None:
    """Buffer one observation for upload. Never raises.

    Called from an activity's ``finally``, so a sink failure has to cost the record
    rather than the activity's real outcome.
    """
    try:
        sink = get_sink()
        if sink is not None:
            sink.add_record(observation)
    # conformance: ignore[E004] telemetry in an activity finally; never fail the activity
    except Exception:
        logger.debug("sizing observation not persisted", exc_info=True)


def _reset_for_testing() -> None:
    """Drop the process-wide sink so a test can build its own."""
    global _sink
    with _sink_lock:
        _sink = None
