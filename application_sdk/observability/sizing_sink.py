"""Durable rows for tier fitting, on the existing ``AtlanObservability`` pipeline.
A histogram bucket cannot give you ``peak_per_input_byte`` per execution.
"""

from __future__ import annotations

import threading
import warnings
from dataclasses import asdict
from time import time
from typing import Any, ClassVar

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    SIZING_BATCH_SIZE,
    SIZING_FILE_NAME,
    SIZING_FLUSH_INTERVAL_SECONDS,
)
from application_sdk.observability.background_loop import BackgroundTask
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.sizing import SIZING_SCHEMA_VERSION
from application_sdk.observability.utils import get_observability_dir

logger = get_logger(__name__)


class SizingObservabilitySink(AtlanObservability[Any]):
    """Buffers observations and uploads them as gzipped NDJSON. The periodic flush
    is NOT optional: ``add_record`` only checks its trigger when a record arrives.
    """

    _flush_task_started: ClassVar[bool] = False
    # Kept so ``_reset_for_testing`` can stop the flush: a reset that only
    # clears the flag leaves the old flush running on the shared loop,
    # writing a dropped sink's rows later against whatever store the
    # environment then names.
    _flush_task: ClassVar[BackgroundTask | None] = None

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Stop the flush and drop every sizing sink's unflushed rows."""
        task = cls._flush_task
        cls._flush_task = None
        cls._flush_task_started = False
        for instance in list(AtlanObservability._instances):
            if isinstance(instance, cls):
                with instance._buffer_lock:
                    instance._buffer = []
                AtlanObservability._instances.remove(instance)
        if task is not None:
            task.cancel()
            # Waits out a flush already in progress, which may be uploading.
            task.wait(timeout=30)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if SizingObservabilitySink._flush_task_started:
            return
        try:
            # On the shared observability loop, whatever thread builds the
            # sink: it is built lazily inside an activity, but must also work
            # off a loop.
            SizingObservabilitySink._flush_task = self._start_periodic_flush()
            SizingObservabilitySink._flush_task_started = True
        # conformance: ignore[E004] telemetry; a sink that cannot start its flush must not stop collection
        except Exception:
            logger.warning("could not start the sizing flush task", exc_info=True)

    def process_record(self, record: Any) -> dict[str, Any]:
        """Flatten one observation into a row. ``app``/``deployment`` are stamped
        here so a row under a shared prefix still says where it came from.
        """
        row: dict[str, Any] = {
            "schema_version": SIZING_SCHEMA_VERSION,
            "app": APPLICATION_NAME,
            "deployment": DEPLOYMENT_NAME,
            # REQUIRED: the base partitions on it and KeyErrors without it. The
            # execution's start, so a row lands in the hour the activity ran.
            "timestamp": record.started_at if record.started_at else time(),
        }
        row.update(asdict(record))
        # Derived here so every consumer computes them identically.
        row["mean_cpu_cores"] = record.mean_cpu_cores
        row["peak_per_input_byte"] = record.peak_per_input_byte
        row["peak_delta_bytes"] = record.peak_delta_bytes
        row["delta_per_input_byte"] = record.delta_per_input_byte
        # Written out, not derived: it decides which model a row feeds, and a
        # consumer that forgets it pools two different quantities.
        row["is_attributable"] = record.is_attributable
        return row

    async def _flush_records(self, records: list[dict[str, Any]]) -> None:
        """Flush, and say so at INFO — the base logs at DEBUG, which every
        deployment filters, leaving "is the sink writing?" unanswerable.
        """
        await super()._flush_records(records)
        if records:
            logger.info("sizing: flushed %d observation(s)", len(records))

    def _store_sink_enabled(self) -> bool:
        """Always on: the shared store-sink flag gates logs/metrics/traces, and an
        app disabling those would silently lose this dataset. Already gated twice.
        """
        return True

    def export_record(self, record: Any) -> None:
        """No-op — histograms come from ``sizing.record_observation``, so a live
        dashboard does not depend on this sink's flush schedule.
        """


_sink: SizingObservabilitySink | None = None
_sink_lock = threading.Lock()


def get_sink() -> SizingObservabilitySink | None:
    """Process-wide sink, built on first use — a worker with collection off should
    not pay for the filesystem touch or the flush task.
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
                    data_dir=get_observability_dir(),
                    file_name=SIZING_FILE_NAME,
                )
            # conformance: ignore[E004] telemetry; a sink that cannot be built must not stop collection
            except Exception:
                logger.debug("sizing sink unavailable", exc_info=True)
                return None
    return _sink


def persist(observation: Any) -> None:
    """Buffer one observation. Never raises: called from an activity's ``finally``,
    so a sink failure must cost the record, not the activity's outcome.
    """
    try:
        sink = get_sink()
        if sink is not None:
            sink.add_record(observation)
    # conformance: ignore[E004] telemetry in an activity finally; never fail the activity
    except Exception:
        logger.debug("sizing observation not persisted", exc_info=True)


async def drain() -> None:
    """Flush the buffer on worker shutdown. These pools scale to zero, so without
    it the last batch of every pod's life is lost.
    """
    sink = _sink
    if sink is None:
        return
    try:
        await sink._flush_buffer(force=True)
    # conformance: ignore[E004] shutdown path; a failed drain must not block shutdown
    except Exception:
        logger.warning("sizing drain failed; buffered rows lost", exc_info=True)


def _reset_for_testing() -> None:
    """Drop the process-wide sink so a test can build its own, and stop its
    flush so nothing the test buffered is written after the test ends.
    """
    global _sink
    # One critical section: a ``get_sink`` that slipped in between dropping
    # ``_sink`` and stopping the flush would get a sink the reset then stops.
    with _sink_lock:
        _sink = None
        SizingObservabilitySink._reset_for_testing()


#: Retention-sweep constants this module used to re-export from
#: ``application_sdk.constants``; served once more by ``__getattr__`` (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "SIZING_RETENTION_DAYS": (
        "an object-store bucket lifecycle rule",
        "the SDK retention sweep never deleted exported sizing files",
    ),
    "SIZING_CLEANUP_ENABLED": (
        "an object-store bucket lifecycle rule",
        "the SDK retention sweep never deleted exported sizing files",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the removed constants once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v3.60.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    from application_sdk.constants import (  # noqa: PLC0415 — only reached on a deprecated access
        _DEPRECATED_CONSTANT_VALUES,
    )

    return _DEPRECATED_CONSTANT_VALUES[name]
