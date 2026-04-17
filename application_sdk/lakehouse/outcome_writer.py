"""Outcome writer for appending processing results to the Iceberg table.

Uses PyIceberg's native table.append() instead of Daft's write_iceberg
because PyIceberg correctly handles vended credentials from Polaris via
fsspec, while Daft's writer uses PyArrow's native S3 filesystem which
doesn't receive the vended credentials.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from application_sdk.lakehouse.models import OutcomeRow, ProcessingResult

if TYPE_CHECKING:
    from pyiceberg.table import Table

logger = logging.getLogger(__name__)


class OutcomeWriter:
    """Buffers OUTCOME rows and flushes to an Iceberg table via PyIceberg."""

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
        # Use the table's own schema to build a correctly-typed Arrow table.
        # This ensures column types match (especially nullable strings that
        # would otherwise be inferred as null type by PyArrow).
        iceberg_schema = self._table.schema()
        arrow_schema = iceberg_schema.as_arrow()
        arrow_table = pa.Table.from_pylist(self._buffer, schema=arrow_schema)
        self._table.append(arrow_table)
        self._buffer.clear()
        logger.info("Flushed %d outcome rows", count)
        return count
