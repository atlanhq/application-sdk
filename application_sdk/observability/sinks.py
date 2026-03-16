"""Observability flush sinks.

Each sink receives a batch of records and writes them to a specific destination.
New destinations are added by creating a new :class:`ObservabilitySink` subclass
and appending it in :class:`~application_sdk.observability.AtlanObservability`'s
constructor — the flush pipeline itself never needs to change.
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from time import time_ns
from typing import Any, Dict, List

import orjson

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    LOG_FILE_NAME,
    METRICS_FILE_NAME,
    TEMPORARY_PATH,
    TRACES_FILE_NAME,
)

# Centralized observability path prefix for MDLH ingestion
OBSERVABILITY_S3_PREFIX = "artifacts/apps/observability/sdr-logs"

# ---------------------------------------------------------------------------
# Record-type enum — replaces brittle filename-string comparisons
# ---------------------------------------------------------------------------


class ObservabilityRecordType(str, Enum):
    LOGS = "logs"
    METRICS = "metrics"
    TRACES = "traces"
    OTHER = "other"


# Evaluated once at import time; values come from env-backed constants.
_FILE_NAME_TO_TYPE: Dict[str, ObservabilityRecordType] = {
    LOG_FILE_NAME: ObservabilityRecordType.LOGS,
    METRICS_FILE_NAME: ObservabilityRecordType.METRICS,
    TRACES_FILE_NAME: ObservabilityRecordType.TRACES,
}


def file_name_to_type(file_name: str) -> ObservabilityRecordType:
    return _FILE_NAME_TO_TYPE.get(file_name, ObservabilityRecordType.OTHER)


# ---------------------------------------------------------------------------
# Shared write-upload primitive
# ---------------------------------------------------------------------------


async def _write_and_upload_json_gz(
    records: List[Dict[str, Any]],
    local_path: str,
    remote_key: str,
    store_names: List[str],
) -> None:
    """Write *records* as NDJSON+gzip, upload to every store, then delete the temp file.

    The local file is always removed in ``finally`` — even when an upload fails —
    so callers never leak disk space regardless of outcome.

    Raises on failure; callers decide whether to isolate the error or abort the batch.
    """
    # ObjectStore is imported lazily to avoid a circular-import at module load.
    from application_sdk.services.objectstore import ObjectStore

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    try:
        # Binary mode: orjson.dumps() returns bytes directly — no decode/encode round-trip.
        with gzip.open(local_path, "wb") as f:
            for record in records:
                f.write(orjson.dumps(record) + b"\n")

        for store_name in store_names:
            await ObjectStore.upload_file(local_path, remote_key, store_name=store_name)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(local_path)


# ---------------------------------------------------------------------------
# Sink ABC
# ---------------------------------------------------------------------------


class ObservabilitySink(ABC):
    """Pluggable destination for a batch of observability records.

    Implementations are configured once at construction time in
    :class:`~application_sdk.observability.AtlanObservability`.  Adding a new
    storage destination never requires touching the flush pipeline.
    """

    @abstractmethod
    async def flush(self, records: List[Dict[str, Any]]) -> None:
        """Persist *records* to this sink's destination.

        Called with the full unpartitioned batch; implementations are responsible
        for partitioning and per-partition error isolation.
        """


# ---------------------------------------------------------------------------
# Sink 1: customer per-app path  (day-partitioned)
# ---------------------------------------------------------------------------


class PartitionedJsonGzSink(ObservabilitySink):
    """Writes hour-partitioned json.gz files to the centralized SDR path.

    Output path::

        {staging_root}/artifacts/apps/observability/sdr-logs/
            year=YYYY/month=MM/day=DD/hour=HH/{epoch_ns}_{deployment}_{app}.json.gz

    This centralized path is used for both customer bucket and Atlan bucket
    (when ENABLE_ATLAN_UPLOAD=true). MDLH S3 pipe reads from Atlan bucket
    and ingests into the shared Iceberg table.

    Supports dual upload (primary + upstream Atlan-managed S3) when more than
    one ``store_name`` is supplied. Partitions are derived from each record's
    own ``timestamp`` field — never from wall-clock time — so late-flushed
    records still land in the correct hour bucket.
    """

    def __init__(
        self,
        record_type: ObservabilityRecordType,
        staging_root: str,
        store_names: List[str],
    ) -> None:
        self._record_type = record_type
        self._staging_root = staging_root
        self._store_names = store_names

    def _partition_dir(self, dt: datetime) -> str:
        return os.path.join(
            self._staging_root,
            OBSERVABILITY_S3_PREFIX,
            f"year={dt.year}",
            f"month={dt.month:02d}",
            f"day={dt.day:02d}",
            f"hour={dt.hour:02d}",
        )

    async def flush(self, records: List[Dict[str, Any]]) -> None:
        if not records:
            return

        # Group records by day using each record's own timestamp.
        partitioned: Dict[str, List[Dict[str, Any]]] = {}
        for record in records:
            dt = datetime.fromtimestamp(record["timestamp"], tz=timezone.utc)
            partitioned.setdefault(self._partition_dir(dt), []).append(record)

        for partition_dir, batch in partitioned.items():
            filename = f"{time_ns()}_{DEPLOYMENT_NAME}_{APPLICATION_NAME}.json.gz"
            local_path = os.path.join(partition_dir, filename)
            # Remote key is relative to the temp root, matching Dapr objectstore convention.
            remote_key = os.path.relpath(local_path, TEMPORARY_PATH)
            try:
                await _write_and_upload_json_gz(
                    batch, local_path, remote_key, self._store_names
                )
                logging.debug(
                    "Exported %d %s records → %s",
                    len(batch),
                    self._record_type.value,
                    remote_key,
                )
            except Exception:
                # Isolate per-partition failures; remaining partitions still proceed.
                logging.exception(
                    "Failed to flush %s partition %s; skipping",
                    self._record_type.value,
                    partition_dir,
                )
