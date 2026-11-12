"""Streaming record I/O: read, write and look up JSONL records without holding a dataset in memory.

An app's memory should not depend on how many records a customer has. The three
shapes that make it depend on that are reading a whole file into a list, building
a whole list before writing it, and keeping a lookup dict of every record. This
module replaces each with a bounded equivalent:

==========================================  ==========================================
Instead of                                  Use
==========================================  ==========================================
``records = read_all(path)``                ``for record in iter_jsonl(path)``
``write_all(path, records)`` after a loop   ``with JsonlWriter(path) as out: out.write(record)``
``lookup = {r["id"]: r for r in records}``  ``KeyedIndex.from_jsonl(path, key="id", fields=[...])``
==========================================  ==========================================

The synchronous functions do blocking file I/O, so inside a task run a whole
read-transform-write pass in one worker thread (``run_in_thread``). When the
records come from awaited calls (one HTTP request per record or per page), use
:func:`aiter_jsonl` and :func:`write_jsonl_async`, which keep the event loop free
and still hold only a bounded batch.
"""

from __future__ import annotations

import os
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
)
from typing import Any, BinaryIO, Self

import orjson

from application_sdk.common.spillable_dict import SpillableDict, SpillableKey
from application_sdk.execution.heartbeat import run_in_thread
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: In-memory budget of a :class:`KeyedIndex` before it moves to disk.
DEFAULT_INDEX_MEMORY_BYTES = 32 * 1024 * 1024


def iter_jsonl(
    path: str | None, *, skip_invalid: bool = False
) -> Generator[dict[str, Any]]:
    """Yield one record per non-blank line of a JSONL file.

    Only the line being parsed is held in memory.

    Args:
        path: Local file path. ``None``, an empty string or a missing file yields
            nothing, so a ``FileReference.local_path`` can be passed unguarded.
        skip_invalid: When true, a line that is not valid JSON is logged and
            skipped. When false (the default) it raises.

    Raises:
        orjson.JSONDecodeError: A line is not valid JSON and ``skip_invalid`` is false.
    """
    if not path or not os.path.isfile(path):
        return
    with open(path, "rb") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield orjson.loads(line)
            except orjson.JSONDecodeError:
                if not skip_invalid:
                    raise
                logger.warning(
                    "Skipping unparseable JSONL line %d in %s",
                    line_number,
                    path,
                    exc_info=True,
                )


class JsonlWriter:
    """Write records to a JSONL file one at a time.

    Each record is serialised and handed to the file as it arrives, so memory
    does not grow with the number of records. The file exists (possibly empty)
    as soon as the writer is opened, so a reader never has to special-case
    "no records".

    Example::

        with JsonlWriter(out_path) as out:
            for record in iter_jsonl(in_path):
                if keep(record):
                    out.write(record)
        return out.count
    """

    def __init__(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self.count = 0
        self.bytes_written = 0
        self._fh: BinaryIO | None = open(path, "wb")  # noqa: SIM115 - closed by close()

    def write(self, record: Mapping[str, Any]) -> None:
        """Append one record as one line."""
        self.write_line(orjson.dumps(record))

    def write_line(self, line: bytes) -> None:
        """Append one already-serialised record. A trailing newline is added if missing."""
        if self._fh is None:
            raise ValueError(f"JsonlWriter for {self.path} is closed")
        if not line.endswith(b"\n"):
            line += b"\n"
        self._fh.write(line)
        self.count += 1
        self.bytes_written += len(line)

    def write_all(self, records: Iterable[Mapping[str, Any]]) -> int:
        """Append every record from an iterable; returns how many were written by this call."""
        before = self.count
        for record in records:
            self.write(record)
        return self.count - before

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def write_jsonl(path: str, records: Iterable[Mapping[str, Any]]) -> int:
    """Write an iterable of records to ``path`` as JSONL and return the record count.

    ``records`` may be a generator, in which case nothing is ever held as a list.
    """
    with JsonlWriter(path) as out:
        return out.write_all(records)


#: Serialised bytes buffered by :func:`write_jsonl_async` between writes.
DEFAULT_ASYNC_FLUSH_BYTES = 1024 * 1024
#: Records read per worker-thread hop by :func:`aiter_jsonl`.
DEFAULT_ASYNC_READ_BATCH = 1000


def _read_batch(
    source: Iterator[dict[str, Any]], batch_size: int
) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    for record in source:
        batch.append(record)
        if len(batch) >= batch_size:
            break
    return batch


async def aiter_jsonl(
    path: str | None,
    *,
    batch_size: int = DEFAULT_ASYNC_READ_BATCH,
    skip_invalid: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Async form of :func:`iter_jsonl`: yields records without blocking the event loop.

    The file is read ``batch_size`` records at a time in a worker thread, so at
    most one batch is in memory.
    """
    source = iter_jsonl(path, skip_invalid=skip_invalid)
    try:
        while True:
            batch = await run_in_thread(_read_batch, source, batch_size)
            if not batch:
                return
            for record in batch:
                yield record
    finally:
        await run_in_thread(source.close)


async def write_jsonl_async(
    path: str,
    records: AsyncIterable[Mapping[str, Any]],
    *,
    flush_bytes: int = DEFAULT_ASYNC_FLUSH_BYTES,
) -> int:
    """Write records from an async source to ``path`` as JSONL and return the record count.

    For records that arrive one awaited call at a time, such as a detail request
    per asset or a page per request. Each record is serialised as it arrives and
    the bytes are written from a worker thread once ``flush_bytes`` have
    accumulated, so memory holds one small buffer rather than every record.

    Example::

        async def details():
            async for summary in aiter_jsonl(summaries_path):
                detail = await client.get_detail(summary["id"])
                if detail is not None:
                    yield detail

        count = await write_jsonl_async(out_path, details())
    """

    def flush(writer: JsonlWriter, lines: list[bytes]) -> None:
        for line in lines:
            writer.write_line(line)

    writer = await run_in_thread(JsonlWriter, path)
    try:
        pending: list[bytes] = []
        pending_bytes = 0
        async for record in records:
            line = orjson.dumps(record)
            pending.append(line)
            pending_bytes += len(line)
            if pending_bytes >= flush_bytes:
                await run_in_thread(flush, writer, pending)
                pending, pending_bytes = [], 0
        if pending:
            await run_in_thread(flush, writer, pending)
        return writer.count
    finally:
        await run_in_thread(writer.close)


_MISSING: Any = object()


class KeyedIndex:
    """A lookup from key to value that stays in memory while small and moves to disk when it grows.

    It starts as a plain ``dict``. Once the stored values pass ``max_memory_bytes``
    the entries move into a :class:`SpillableDict` (RocksDB in a temp directory) and
    later entries go straight there, so memory stops growing with the number of
    records. Small runs never touch the disk.

    Store only the fields the lookup needs. An index that holds whole parent
    records to read one field of them is the most common reason a lookup grows.

    Differences from ``dict`` that callers must respect:

    * A value read after the index moved to disk is a copy. Mutating it does not
      change what is stored; assign it back to the key.
    * Iteration order is not insertion order once on disk.
    * Keys must be ``str``, ``int``, ``float``, ``bool`` or ``bytes``.
    * Not thread-safe.

    Example::

        with KeyedIndex.from_jsonl(collections_path, key="id", fields=["id", "name"]) as collections:
            for question in iter_jsonl(questions_path):
                parent = collections.get(question["collection_id"])
    """

    def __init__(self, *, max_memory_bytes: int = DEFAULT_INDEX_MEMORY_BYTES) -> None:
        self._memory: dict[SpillableKey, Any] | None = {}
        self._disk: SpillableDict | None = None
        self._max_memory_bytes = max_memory_bytes
        self._memory_bytes = 0

    @classmethod
    def from_jsonl(
        cls,
        path: str | None,
        *,
        key: str | Callable[[dict[str, Any]], SpillableKey | None],
        fields: Iterable[str] | None = None,
        value: Callable[[dict[str, Any]], Any] | None = None,
        max_memory_bytes: int = DEFAULT_INDEX_MEMORY_BYTES,
    ) -> Self:
        """Build an index by streaming a JSONL file.

        Args:
            path: JSONL file; a missing path gives an empty index.
            key: Field name, or a function of the record. A record whose key is
                ``None`` is left out.
            fields: Keep only these fields of each record as the value.
            value: Function of the record that returns the value to store.
                Mutually exclusive with ``fields``. With neither, the whole
                record is stored.
            max_memory_bytes: In-memory budget before the index moves to disk.
        """
        if fields is not None and value is not None:
            raise ValueError("pass either 'fields' or 'value', not both")
        wanted = list(fields) if fields is not None else None
        index = cls(max_memory_bytes=max_memory_bytes)
        for record in iter_jsonl(path):
            record_key = record.get(key) if isinstance(key, str) else key(record)
            if record_key is None:
                continue
            if value is not None:
                index[record_key] = value(record)
            elif wanted is not None:
                index[record_key] = {
                    name: record[name] for name in wanted if name in record
                }
            else:
                index[record_key] = record
        return index

    @property
    def spilled(self) -> bool:
        """Whether the index has moved to disk."""
        return self._disk is not None

    def __setitem__(self, key: SpillableKey, value: Any) -> None:
        if self._memory is None:
            if self._disk is None:
                raise ValueError("KeyedIndex is closed")
            self._disk[key] = value
            return
        self._memory[key] = value
        self._memory_bytes += _approximate_size(value)
        if self._memory_bytes > self._max_memory_bytes:
            self._spill()

    def __getitem__(self, key: SpillableKey) -> Any:
        found = self.get(key, _MISSING)
        if found is _MISSING:
            raise KeyError(key)
        return found

    def get(self, key: Any, default: Any = None) -> Any:
        if self._memory is not None:
            try:
                return self._memory.get(key, default)
            except TypeError:  # unhashable key: not present, same answer as on disk
                return default
        if self._disk is None:
            return default
        return self._disk.get(key, default)

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __len__(self) -> int:
        if self._memory is not None:
            return len(self._memory)
        return len(self._disk) if self._disk is not None else 0

    def append(self, key: SpillableKey, item: Any) -> None:
        """Append ``item`` to the list stored at ``key``, creating the list if absent."""
        if self._memory is not None:
            self._memory.setdefault(key, []).append(item)
            self._memory_bytes += _approximate_size(item)
            if self._memory_bytes > self._max_memory_bytes:
                self._spill()
            return
        if self._disk is None:
            raise ValueError("KeyedIndex is closed")
        self._disk.append_to_key(key, item)

    def _spill(self) -> None:
        disk = SpillableDict()
        for key, value in (self._memory or {}).items():
            disk[key] = value
        logger.info(
            "KeyedIndex moved to disk: entries=%d approx_bytes=%d",
            len(self._memory or {}),
            self._memory_bytes,
        )
        self._disk = disk
        self._memory = None

    def close(self) -> None:
        if self._disk is not None:
            self._disk.close()
            self._disk = None
        self._memory = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _approximate_size(value: Any) -> int:
    """A cheap estimate of the memory a stored value takes, from its serialised length.

    A Python object costs several times its JSON length, so this undercounts; the
    multiplier keeps the budget honest without walking the object graph.
    """
    if isinstance(value, (str, bytes)):
        return len(value) + 64
    if isinstance(value, (int, float, bool)) or value is None:
        return 32
    try:
        return len(orjson.dumps(value)) * 4 + 64
    except TypeError:
        return 1024
