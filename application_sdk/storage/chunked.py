"""Format-agnostic time-based chunked writer.

Replacement primitive for the v4.0-deprecated ``ParquetFileWriter`` /
``JsonFileWriter``. Rolls over to a new output file every ``chunk_interval_seconds``
of wall clock — *not* every N records. Time-based rollover gives a predictable
Temporal heartbeat cadence regardless of upstream rate:

* A slow JDBC fetch streaming 200 rows/min and a fast in-memory msgspec
  transform streaming 10 000 records/ms both checkpoint roughly once per
  ``chunk_interval_seconds``.
* No risk of producing a single multi-GB file at the end of a long
  activity (which would block heartbeats during the final flush).
* No need for callers to guess a "right" record count threshold; pick a
  time budget that matches the activity's heartbeat timeout.

Format is open. The caller supplies a ``flush_fn(buffer, path)`` that knows
how to serialise the buffered batches to a single output file, so the same
helper drives parquet, JSON, CSV, msgpack, or anything else.

Example — parquet (pandas)::

    import pandas as pd
    from application_sdk.contracts.types import FileReference
    from application_sdk.storage.chunked import TimeChunkedWriter

    def _flush_parquet(batches: list[pd.DataFrame], path: str) -> None:
        pd.concat(batches, ignore_index=True).to_parquet(path)

    async with TimeChunkedWriter[pd.DataFrame](
        base_path="/tmp/extract",
        extension=".parquet",
        flush_fn=_flush_parquet,
        chunk_interval_seconds=60.0,
    ) as writer:
        async for df in stream_dataframes():
            await writer.append(df)
    return MyOutput(data=writer.file_reference)

Example — JSON (line-delimited)::

    import orjson
    from application_sdk.contracts.types import FileReference
    from application_sdk.storage.chunked import TimeChunkedWriter

    def _flush_jsonl(batches: list[list[dict]], path: str) -> None:
        with open(path, "wb") as f:
            for batch in batches:
                for record in batch:
                    f.write(orjson.dumps(record))
                    f.write(b"\\n")

    async with TimeChunkedWriter[list[dict]](
        base_path="/tmp/extract",
        extension=".json",
        flush_fn=_flush_jsonl,
        chunk_interval_seconds=60.0,
    ) as writer:
        async for records in stream_records():
            await writer.append(records)
    return MyOutput(data=writer.file_reference)
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from application_sdk.common.file_ops import SafeFileOps
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    # Avoid the circular import: contracts.types ← observability ←
    # storage.__init__ ← chunked. The runtime reference inside the
    # file_reference property uses a lazy import.
    from application_sdk.contracts.types import FileReference

logger = get_logger(__name__)

T = TypeVar("T")

FlushFn = Callable[[list[T], str], Awaitable[None] | None]
ChunkCompleteFn = Callable[[int, str], Awaitable[None] | None]


class TimeChunkedWriter(Generic[T]):
    """Roll over to a new chunk file every ``chunk_interval_seconds``.

    Buffers appended batches in memory; rolls them to a single new chunk
    file on disk once the wall-clock interval since the current chunk's
    first append has elapsed (or on ``__aexit__`` / explicit ``flush()``).

    Args:
        base_path: Parent directory under which the writer creates its own
            scoped sub-directory. The returned :pyattr:`file_reference`
            covers only that sub-directory, so it is safe to pass a shared
            path (``/tmp`` etc.) — sibling content is never included in
            the upload.
        extension: File suffix for each chunk (e.g. ``".parquet"``,
            ``".json"``). Include the leading dot.
        flush_fn: Callback ``(buffered_batches, output_path) -> None`` (sync
            or async) that serialises the current buffer to a single file.
            Receives all batches accumulated since the last rollover so the
            caller controls how they are combined (e.g. ``pd.concat`` for
            parquet, line-delimited writes for JSONL).
        chunk_interval_seconds: Wall-clock interval after which the next
            ``append`` triggers a rollover. Defaults to 60 seconds — a
            sensible match for typical Temporal heartbeat timeouts.
        on_chunk_complete: Optional callback ``(chunk_index, chunk_path) ->
            None`` (sync or async) invoked after every successful chunk
            flush. Useful for Temporal heartbeats — pass
            ``lambda idx, path: activity.heartbeat(f"chunk {idx}")``.
        scoped_subdir_name: Optional explicit name for the writer-owned
            sub-directory. Defaults to ``f"chunked_{uuid.uuid4().hex[:8]}"``.
            Override when the directory name needs to be deterministic
            (e.g. for tests).
    """

    def __init__(
        self,
        base_path: str,
        extension: str,
        flush_fn: FlushFn[T],
        *,
        chunk_interval_seconds: float = 60.0,
        on_chunk_complete: ChunkCompleteFn | None = None,
        scoped_subdir_name: str | None = None,
    ) -> None:
        if not base_path:
            raise ValueError("base_path is required")
        if not extension.startswith("."):
            raise ValueError(f"extension must start with '.', got {extension!r}")
        if chunk_interval_seconds <= 0:
            raise ValueError(
                "chunk_interval_seconds must be > 0; got " f"{chunk_interval_seconds}"
            )

        self.output_dir = os.path.join(
            base_path,
            scoped_subdir_name or f"chunked_{uuid.uuid4().hex[:8]}",
        )
        SafeFileOps.makedirs(self.output_dir, exist_ok=True)

        self.extension = extension
        self.flush_fn = flush_fn
        self.chunk_interval_seconds = chunk_interval_seconds
        self.on_chunk_complete = on_chunk_complete

        self._buffer: list[T] = []
        self._chunk_start_monotonic: float | None = None
        self._chunk_index = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> TimeChunkedWriter[T]:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def append(self, batch: T) -> None:
        """Append a batch; trigger a rollover if the interval has elapsed.

        Thread-safe within a single event loop via an internal asyncio Lock.
        """
        if self._closed:
            raise RuntimeError("Cannot append to a closed TimeChunkedWriter")

        async with self._lock:
            if self._chunk_start_monotonic is None:
                self._chunk_start_monotonic = time.monotonic()
            self._buffer.append(batch)

            elapsed = time.monotonic() - self._chunk_start_monotonic
            if elapsed >= self.chunk_interval_seconds:
                await self._flush_locked()

    async def flush(self) -> None:
        """Force-flush the current buffer to a new chunk file.

        Useful at activity boundaries when the caller wants a known
        checkpoint regardless of elapsed time.
        """
        async with self._lock:
            await self._flush_locked()

    async def close(self) -> None:
        """Flush the remaining buffer and mark the writer closed.

        Idempotent — safe to call multiple times. Called automatically by
        ``__aexit__``.
        """
        async with self._lock:
            if self._closed:
                return
            await self._flush_locked()
            self._closed = True

    async def _flush_locked(self) -> None:
        """Internal flush. Caller must hold ``self._lock``."""
        if not self._buffer:
            return

        chunk_path = os.path.join(
            self.output_dir, f"chunk-{self._chunk_index}{self.extension}"
        )
        try:
            result = self.flush_fn(self._buffer, chunk_path)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.error(
                "TimeChunkedWriter flush failed",
                exc_info=True,
                extra={
                    "chunk_index": self._chunk_index,
                    "chunk_path": chunk_path,
                    "buffered_batches": len(self._buffer),
                },
            )
            raise

        completed_index = self._chunk_index
        self._chunk_index += 1
        self._buffer = []
        self._chunk_start_monotonic = None

        if self.on_chunk_complete is not None:
            try:
                cb_result = self.on_chunk_complete(completed_index, chunk_path)
                if inspect.isawaitable(cb_result):
                    await cb_result
            except Exception:
                # on_chunk_complete is best-effort (heartbeat etc.); never
                # let it abort the write loop.
                logger.warning(
                    "TimeChunkedWriter on_chunk_complete callback raised",
                    exc_info=True,
                )

    @property
    def file_reference(self) -> FileReference:
        """Ephemeral FileReference covering this writer's output directory.

        Return this from your task's typed Output and the Temporal activity
        interceptor uploads the directory on task return (SHA-256 sidecars
        + parallel transfers). No caller-side ``persist_file_reference``
        call is required.
        """
        from application_sdk.contracts.types import (  # noqa: PLC0415 — lazy: see TYPE_CHECKING block at top
            FileReference,
        )

        return FileReference.from_local(self.output_dir)

    @property
    def chunk_count(self) -> int:
        """Number of chunk files written so far."""
        return self._chunk_index
