"""Streaming Parquet I/O: read, rewrite and write Parquet files one bounded batch at a time.

The Parquet counterpart of :mod:`application_sdk.common.records`. Reading a
file with ``pq.read_table`` and writing it back with ``pq.write_table`` holds
the whole file in memory, and turning its columns into Python lists holds it
several times over. These helpers do the same work a batch at a time, with the
batch sized in bytes so that wide rows do not raise the peak:

==============================================  =====================================
Instead of                                      Use
==============================================  =====================================
``table = pq.read_table(path)``                 ``for batch in iter_parquet_batches(path)``
``pq.write_table(change(read_table(p)), p)``    ``rewrite_parquet(p, change)``
``pq.write_table(pa.table(all_rows), path)``    ``with ParquetBatchWriter(path, schema) as out``
==============================================  =====================================

Memory is bounded by one batch, with one caveat that belongs to the file, not
to these helpers: Parquet is decoded a row group at a time, so a file written
with very large row groups still costs one row group to read. Files written by
:class:`ParquetBatchWriter` and :func:`rewrite_parquet` have one row group per
batch, so they are cheap to read back.

Requires ``pyarrow``. Everything here does blocking file I/O; inside a task,
run a whole pass in one worker thread (``run_in_thread``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow
    import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow

#: Uncompressed bytes a batch aims for when reading.
DEFAULT_BATCH_BYTES = 32 * 1024 * 1024
#: Row-count bounds on a batch, whatever the row width.
MIN_BATCH_ROWS = 1024
MAX_BATCH_ROWS = 131_072


def parquet_batch_rows(
    parquet_file: pq.ParquetFile, target_bytes: int = DEFAULT_BATCH_BYTES
) -> int:
    """How many rows of ``parquet_file`` make a batch of about ``target_bytes``.

    Uses the uncompressed size recorded in the file's footer, so no data is
    read. The result is clamped to ``MIN_BATCH_ROWS``..``MAX_BATCH_ROWS``.
    """
    meta = parquet_file.metadata
    if meta.num_rows == 0:
        return MIN_BATCH_ROWS
    total = sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups))
    bytes_per_row = max(1.0, total / meta.num_rows)
    return max(MIN_BATCH_ROWS, min(MAX_BATCH_ROWS, int(target_bytes / bytes_per_row)))


def iter_parquet_batches(
    path: str,
    *,
    columns: Sequence[str] | None = None,
    target_bytes: int = DEFAULT_BATCH_BYTES,
) -> Iterator[pa.RecordBatch]:
    """Yield the rows of a Parquet file as record batches of about ``target_bytes``.

    Args:
        path: Local Parquet file.
        columns: Read only these columns. Naming the few columns a pass needs is
            the cheapest memory saving there is.
        target_bytes: Uncompressed size a batch aims for.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow

    parquet_file = pq.ParquetFile(path)
    try:
        yield from parquet_file.iter_batches(
            batch_size=parquet_batch_rows(parquet_file, target_bytes),
            columns=list(columns) if columns is not None else None,
        )
    finally:
        parquet_file.close()


def rewrite_parquet(
    path: str,
    transform: Callable[[pa.RecordBatch], pa.RecordBatch | None],
    *,
    target_bytes: int = DEFAULT_BATCH_BYTES,
) -> tuple[int, int]:
    """Rewrite a Parquet file in place by passing each batch through ``transform``.

    The file is read a batch at a time, each transformed batch is written to a
    temporary file beside it, and the temporary file replaces the original only
    once every batch has been written. A failure part-way leaves the original
    untouched.

    Args:
        path: Local Parquet file to rewrite.
        transform: Receives one record batch and returns the batch to write in
            its place (fewer rows, added columns, changed values) or ``None`` to
            drop the batch. Every returned batch must have the same schema.
        target_bytes: Uncompressed size a batch aims for.

    Returns:
        ``(rows read, rows written)``. A file with no rows is left as it is.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow

    # Unique, and not ending in ".parquet": a concurrent rewrite of the same file
    # cannot collide with it, and a reader globbing "*.parquet" never sees it.
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    rows_in = rows_out = 0
    writer: pq.ParquetWriter | None = None
    try:
        for batch in iter_parquet_batches(path, target_bytes=target_bytes):
            rows_in += batch.num_rows
            out = transform(batch)
            if out is None:
                continue
            if writer is None:
                writer = pq.ParquetWriter(tmp, out.schema)
            writer.write_batch(out)
            rows_out += out.num_rows
        if writer is None:
            if rows_in:
                # Every batch was dropped: keep the schema, lose the rows.
                schema = pq.read_schema(path)
                pq.ParquetWriter(tmp, schema).close()
                os.replace(tmp, path)
            return rows_in, rows_out
        writer.close()
        writer = None
        os.replace(tmp, path)
        return rows_in, rows_out
    finally:
        if writer is not None:
            writer.close()
        if os.path.exists(tmp):
            os.remove(tmp)


class ParquetBatchWriter:
    """Write rows to a Parquet file in bounded batches.

    Rows are buffered as columns and flushed as one row group whenever the
    buffered values pass ``flush_bytes``, so memory holds one batch however many
    rows are written. The file always exists with the declared schema, even when
    no row is written, so a reader never has to special-case "no rows".

    Example::

        schema = pa.schema([("QUALIFIED_NAME", pa.string()), ("DEFINITION", pa.string())])
        with ParquetBatchWriter(out_path, schema) as out:
            for entity in iter_jsonl(in_path):
                out.write_row({"QUALIFIED_NAME": entity["qn"], "DEFINITION": entity["ddl"]})
        return out.count
    """

    def __init__(
        self, path: str, schema: pa.Schema, *, flush_bytes: int = 8 * 1024 * 1024
    ) -> None:
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self.schema = schema
        self.count = 0
        self._flush_bytes = flush_bytes
        self._columns: dict[str, list[Any]] = {name: [] for name in schema.names}
        self._pending_bytes = 0
        self._writer: pq.ParquetWriter | None = pq.ParquetWriter(path, schema)

    def write_row(self, row: Mapping[str, Any]) -> None:
        """Buffer one row. A column the row does not have is written as null."""
        if self._writer is None:
            raise ValueError(f"ParquetBatchWriter for {self.path} is closed")
        for name, values in self._columns.items():
            value = row.get(name)
            values.append(value)
            self._pending_bytes += len(value) if isinstance(value, (str, bytes)) else 8
        self.count += 1
        if self._pending_bytes >= self._flush_bytes:
            self.flush()

    def write_batch(self, batch: pa.RecordBatch) -> None:
        """Write an Arrow record batch as its own row group, after any buffered rows."""
        if self._writer is None:
            raise ValueError(f"ParquetBatchWriter for {self.path} is closed")
        self.flush()
        self._writer.write_batch(batch)
        self.count += batch.num_rows

    def flush(self) -> None:
        """Write the buffered rows as one row group."""
        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

        if (
            self._writer is None
            or not self._columns
            or not next(iter(self._columns.values()))
        ):
            return
        self._writer.write_table(pa.table(self._columns, schema=self.schema))
        self._columns = {name: [] for name in self.schema.names}
        self._pending_bytes = 0

    def close(self) -> None:
        if self._writer is not None:
            self.flush()
            self._writer.close()
            self._writer = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
