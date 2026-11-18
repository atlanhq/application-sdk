"""Stream records row to row, and size batches in bytes rather than rows.

A transform that loads a whole file — into a list, or into a DataFrame — needs
memory proportional to the tenant, so the same activity that fits a small
tenant runs a large one out of memory. These helpers keep one record in memory
at a time, so an activity's memory is the same for ten thousand records as for
ten million:

* :func:`iter_jsonl` reads JSON-lines files one record at a time.
* :func:`map_records` / :func:`map_jsonl` read a record, map it, write it, and
  move on; :func:`write_records` is the same loop into a handle the caller
  already holds. This is the loop :class:`~application_sdk.templates.SqlApp`
  runs for every ``transform_*`` task, published for connectors that are not
  ``SqlApp``.
* :func:`batched_by_bytes` / :func:`iter_jsonl_batches` group records for code
  that genuinely needs a batch (a bulk API call, a bounded join). A row count is
  the wrong unit for that: the same 100,000 rows can be 10 MiB for one table and
  1 GiB for another. A batch here closes at a byte budget,
  :data:`DEFAULT_BATCH_BYTES` unless the caller passes another.
  :func:`serialized_size` is the default measure of one record.

Example — a connector transform::

    from application_sdk.common.asset_serialization import entity_bytes
    from application_sdk.common.last_sync import resolve_last_sync_details
    from application_sdk.storage.streaming import map_jsonl

    # Resolved on the event loop, once: it reads the activity's execution
    # context, and every entity of the run carries the same lastSync values.
    last_sync = resolve_last_sync_details()
    result = await map_jsonl(
        raw_path,
        transformed_path,
        lambda record: entity_bytes(
            map_table(record),
            connection_name=name,
            last_sync=last_sync,
            entity_type="table",
        ),
    )
    return TransformOutput(total_record_count=result.written)

:func:`map_records` and :func:`map_jsonl` write their output atomically: its
final name holds either the complete output or whatever was there before,
never a partial file a retry or a downstream activity could mistake for a
finished one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypeVar, Union

import orjson

from application_sdk._runtime.offload import raise_if_offload_abandoned, run_in_thread
from application_sdk.common.asset_serialization import orjson_default
from application_sdk.common.atomic import atomic_write
from application_sdk.storage.streaming_errors import InvalidBatchBudgetError

T = TypeVar("T")

#: Default batch budget: 32 MiB of serialized records. Big enough that a batch
#: is thousands of typical metadata records, so per-batch overhead (a request,
#: a file, a heartbeat) stays negligible; small enough that the in-memory form
#: of a batch — several times its serialized size as Python objects — stays a
#: fixed cost per activity rather than a per-tenant one.
DEFAULT_BATCH_BYTES: int = 32 * 1024 * 1024

#: Records between cancellation checks inside a map. A cancelled activity's
#: worker thread cannot be killed, so the loop polls; per-record polling would
#: be a hot-path cost for no extra signal.
_CANCEL_CHECK_INTERVAL = 1024

PathLike = Union[str, "os.PathLike[str]"]
#: What a map function may return for one record: a mapping (serialized with
#: orjson), an already serialized line without its trailing newline (``bytes``,
#: or ``str``, written as UTF-8), or ``None`` to drop the record.
MapOutput = Union[Mapping[str, Any], bytes, str, None]


@dataclass(frozen=True)
class MapResult:
    """Counts from one :func:`map_records` / :func:`map_jsonl` run.

    Attributes:
        read: Records consumed from the input.
        written: Lines written to the output — ``read`` minus the records the
            map function dropped by returning ``None``.
    """

    read: int
    written: int


def _as_paths(paths: PathLike | Iterable[PathLike]) -> list[PathLike]:
    if isinstance(paths, (str, os.PathLike)):
        return [paths]
    return list(paths)


def _iter_lines(paths: PathLike | Iterable[PathLike]) -> Iterator[bytes]:
    """Yield each non-blank line of *paths*, stripped, one file after another."""
    for path in _as_paths(paths):
        with open(path, "rb") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line


def iter_jsonl(paths: PathLike | Iterable[PathLike]) -> Iterator[dict[str, Any]]:
    """Yield the records of one or more JSON-lines files, one at a time.

    Blank and whitespace-only lines are skipped. Files are read in the order
    given, and only one line is held at a time.

    Args:
        paths: A file path, or several to read one after another.

    Raises:
        orjson.JSONDecodeError: A non-blank line is not valid JSON.
    """
    return map(orjson.loads, _iter_lines(paths))


def serialized_size(record: Any) -> int:
    """Bytes *record* takes as one JSON line, newline included."""
    return len(orjson.dumps(record, default=orjson_default)) + 1


def batched_by_bytes(
    items: Iterable[T],
    max_bytes: int = DEFAULT_BATCH_BYTES,
    *,
    size_of: Callable[[T], int] = serialized_size,
) -> Iterator[list[T]]:
    """Group *items* into lists of at most *max_bytes*, in order.

    A batch is closed before an item would take it past the budget. An item
    larger than the budget on its own is yielded as a batch of one rather than
    dropped or split.

    Args:
        items: Any iterable; consumed lazily.
        max_bytes: Byte budget per batch.
        size_of: Size of one item in bytes. Defaults to its serialized JSON
            length; pass a cheaper measure when one is at hand (the length of
            a line already read, ``len`` of bytes).

    Raises:
        InvalidBatchBudgetError: *max_bytes* is not positive.
    """
    if max_bytes <= 0:
        raise InvalidBatchBudgetError(received_value=str(max_bytes))
    return _batches(items, max_bytes, size_of)


def _batches(
    items: Iterable[T], max_bytes: int, size_of: Callable[[T], int]
) -> Iterator[list[T]]:
    batch: list[T] = []
    batch_bytes = 0
    for item in items:
        size = size_of(item)
        if batch and batch_bytes + size > max_bytes:
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(item)
        batch_bytes += size
    if batch:
        yield batch


def iter_jsonl_batches(
    paths: PathLike | Iterable[PathLike],
    max_bytes: int = DEFAULT_BATCH_BYTES,
) -> Iterator[list[dict[str, Any]]]:
    """Yield the records of JSON-lines files in batches of at most *max_bytes*.

    Each record is sized by its serialized line (stripped, plus one byte for
    the newline), so batching costs no extra serialization.

    Raises:
        InvalidBatchBudgetError: *max_bytes* is not positive.
    """
    lines = batched_by_bytes(
        _iter_lines(paths), max_bytes, size_of=lambda line: len(line) + 1
    )
    return ([orjson.loads(line) for line in batch] for batch in lines)


def write_records(
    records: Iterable[T],
    out: BinaryIO,
    fn: Callable[[T], MapOutput],
    *,
    default: Callable[[Any], Any] = orjson_default,
) -> MapResult:
    """Map *records* one at a time and write each result as a line to *out*.

    The caller owns *out* — opening, closing, and whether the file it backs
    is published atomically. :func:`map_records` is this plus an atomic
    output file, and is what to reach for unless the handle is already open.

    Blocking: call it from a worker thread (``run_in_thread``).

    Args:
        records: The input, consumed lazily — pass a generator such as
            :func:`iter_jsonl` so the input is never held whole.
        out: A binary handle open for writing.
        fn: Called once per record. Return a mapping to have it serialized,
            ``bytes`` or ``str`` that is already one serialized line (no
            trailing newline), or ``None`` to drop the record.
        default: orjson ``default=`` hook for values orjson cannot serialize
            itself. The default handles ``Decimal`` and ``bytes`` the way the
            SQL extract side does and raises on anything else.

    Returns:
        How many records were read and how many lines were written.

    Raises:
        asyncio.CancelledError: Running under ``run_in_thread`` and the caller
            stopped waiting; checked every 1,024 records.
    """
    write = out.write
    dumps = orjson.dumps
    append_newline = orjson.OPT_APPEND_NEWLINE
    read = written = 0
    for record in records:
        read += 1
        result = fn(record)
        if result is not None:
            if isinstance(result, (bytes, bytearray, memoryview)):
                write(result)
                write(b"\n")
            elif isinstance(result, str):
                write(result.encode())
                write(b"\n")
            else:
                write(dumps(result, default=default, option=append_newline))
            written += 1
        if not read % _CANCEL_CHECK_INTERVAL:
            raise_if_offload_abandoned()
    return MapResult(read=read, written=written)


def map_records(
    records: Iterable[T],
    dst: PathLike,
    fn: Callable[[T], MapOutput],
    *,
    default: Callable[[Any], Any] = orjson_default,
    operation: str = "record map",
) -> MapResult:
    """Map *records* one at a time into the JSON-lines file *dst*.

    *dst* is written atomically: its final name holds either the complete
    output or whatever was there before. Blocking: call it from a worker
    thread (``run_in_thread``), or use :func:`map_jsonl`, which does that for
    a JSON-lines input.

    Args:
        records: The input, consumed lazily.
        dst: Output file. Its parent directory is created.
        fn: Per-record map; see :func:`write_records`.
        default: orjson ``default=`` hook; see :func:`write_records`.
        operation: Short phrase naming the step, used in a disk-full error.

    Returns:
        How many records were read and how many lines were written.
    """
    with atomic_write(dst, operation=operation) as out:
        return write_records(records, out, fn, default=default)


def _map_jsonl_file(
    src: PathLike | list[PathLike],
    dst: PathLike,
    fn: Callable[[dict[str, Any]], MapOutput],
    default: Callable[[Any], Any],
    operation: str,
) -> MapResult:
    return map_records(iter_jsonl(src), dst, fn, default=default, operation=operation)


async def map_jsonl(
    src: PathLike | Iterable[PathLike],
    dst: PathLike,
    fn: Callable[[dict[str, Any]], MapOutput],
    *,
    default: Callable[[Any], Any] = orjson_default,
    operation: str = "jsonl map",
) -> MapResult:
    """Map the records of JSON-lines *src* into JSON-lines *dst*, row to row.

    The whole map runs as one ``run_in_thread`` call, so the event loop — and
    the activity heartbeat — stays free however large the input is, and the
    stall watchdog treats the call as held for its duration.

    Args:
        src: Input file, or several read one after another.
        dst: Output file, written atomically.
        fn: Per-record map; see :func:`map_records`.
        default: orjson ``default=`` hook; see :func:`map_records`.
        operation: Short phrase naming the step, used in a disk-full error.

    Returns:
        How many records were read and how many lines were written.
    """
    sources = _as_paths(src)
    return await run_in_thread(
        _map_jsonl_file, sources, Path(dst), fn, default, operation
    )
