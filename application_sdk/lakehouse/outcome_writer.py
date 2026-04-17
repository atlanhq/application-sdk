"""Outcome writer for appending processing results to the Iceberg table."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import daft

from application_sdk.lakehouse.models import OutcomeRow, ProcessingResult

if TYPE_CHECKING:
    from pyiceberg.table import Table

logger = logging.getLogger(__name__)


class OutcomeWriter:
    """Buffers OUTCOME rows and flushes to an Iceberg table via Daft."""

    def __init__(self, table: "Table") -> None:
        self._table = table
        self._buffer: list[dict[str, Any]] = []

    def add_outcome(
        self,
        event: dict[str, Any],
        result: ProcessingResult,
        retry_count: int,
    ) -> None:
        actual_retry_count = (
            retry_count + 1 if result.status == "RETRY" else retry_count
        )
        row = OutcomeRow.from_event(event, result, retry_count=actual_retry_count)
        self._buffer.append(row.to_dict())

    def flush(self) -> int:
        if not self._buffer:
            return 0

        count = len(self._buffer)
        df = daft.from_pylist(list(self._buffer))
        df.write_iceberg(self._table, mode="append")
        self._buffer.clear()
        logger.info("Flushed %d outcome rows", count)
        return count
