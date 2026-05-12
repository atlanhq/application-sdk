"""Format-agnostic rolling file writer.

``RollingFileWriter`` is the recommended replacement for the v4.0-deprecated
``ParquetFileWriter`` / ``JsonFileWriter``. It rolls over to a new output
file whenever any of its configured rollover policies fire — time elapsed,
buffer size in bytes, buffer size in records, or any composition of those.

The default policy bundle (time + bytes) gives a predictable Temporal
heartbeat cadence regardless of upstream rate while bounding memory:

* A slow JDBC fetch streaming 200 rows/min checkpoints once per
  ``chunk_interval_seconds`` (default 30 s).
* A fast in-memory msgspec transform streaming 10 000 records/ms hits the
  byte ceiling first (default 50 MB) and rolls over before the time
  interval — bounding peak memory.
* Apps that think in rows can opt into ``max_buffer_records`` for a third
  guard.

Format is open. The caller supplies a ``flush_fn(buffer, path)`` that knows
how to serialise the buffered batches to a single output file, so the same
helper drives parquet, JSON, CSV, msgpack, or anything else.

The name follows the standard logging-world convention (Python's
``logging.handlers.RotatingFileHandler`` / ``TimedRotatingFileHandler``,
log4j's ``RollingFileAppender``, logback's ``RollingPolicy``) — anyone
familiar with server-side logging recognises what a "rolling file" is.

Example — parquet (pandas), default policy bundle::

    import pandas as pd
    from application_sdk.contracts.types import FileReference
    from application_sdk.storage.rolling import RollingFileWriter

    def _flush_parquet(batches: list[pd.DataFrame], path: str) -> None:
        pd.concat(batches, ignore_index=True).to_parquet(path)

    async with RollingFileWriter[pd.DataFrame](
        base_path="/tmp/extract",
        extension=".parquet",
        flush_fn=_flush_parquet,
        # chunk_interval_seconds=30.0       # default
        # max_buffer_bytes=50 * 1024 * 1024 # default 50 MB
        # max_buffer_records=None           # default; opt-in
    ) as writer:
        async for df in stream_dataframes():
            await writer.append(df)
    return MyOutput(data=writer.file_reference)

Example — custom policy (e.g. size-only for a fixed-size export)::

    from application_sdk.storage.rolling import (
        RollingFileWriter, SizePolicy,
    )

    async with RollingFileWriter[bytes](
        base_path="/tmp/export",
        extension=".bin",
        flush_fn=_flush_bytes,
        rollover_policy=SizePolicy(max_bytes=64 * 1024 * 1024),
    ) as writer:
        ...
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from application_sdk.common.file_ops import SafeFileOps
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

FlushFn = Callable[[list[T], str], Awaitable[None] | None]
ChunkCompleteFn = Callable[[int, str], Awaitable[None] | None]
SizeEstimatorFn = Callable[[Any], int]

# Default ceiling on in-memory buffer size between rollovers.
# Conservative for pod-constrained environments (Automation Engine typically
# runs tasks at 256-512 MB); 50 MB leaves plenty of headroom while catching
# runaway-fast-upstream scenarios before they OOM.
_DEFAULT_MAX_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB

# Default rollover cadence. Halved from a naïve 60 s to align better with
# typical Temporal heartbeat timeouts; still generous for slow JDBC streams
# (a 200 rows/min source produces ~100 rows per chunk at 30 s).
_DEFAULT_CHUNK_INTERVAL_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Buffer state + rollover policies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BufferState:
    """Snapshot of the writer's buffer at the moment a rollover decision is made.

    Passed to ``RolloverPolicy.should_roll`` after each ``append``. Policies
    inspect whichever fields they care about — time-based looks at
    ``chunk_start_monotonic``, size-based at ``buffered_bytes``, etc.

    Attributes:
        chunk_start_monotonic: ``time.monotonic()`` reading when the current
            chunk's first append happened. ``None`` between flushes.
        buffered_batches: Number of items currently held in the in-memory
            buffer (count of ``append`` calls since the last flush).
        buffered_bytes: Best-effort byte estimate of the buffer's payload
            (sum of per-batch estimates from the configured size estimator).
        buffered_records: Sum of per-batch record counts since the last
            flush. ``len(batch)`` when batches are sized; ``1`` otherwise.
    """

    chunk_start_monotonic: float
    buffered_batches: int
    buffered_bytes: int
    buffered_records: int


class RolloverPolicy(ABC):
    """Decides when ``RollingFileWriter`` should flush the current buffer."""

    @abstractmethod
    def should_roll(self, state: BufferState) -> bool:
        """Return True to trigger a rollover after the current ``append``."""

    def on_flush(self) -> None:
        """Hook fired after every successful flush.

        Override to reset any policy-internal state (e.g. a counter that
        tracks events since the last rollover). The default is a no-op
        because the writer already resets the canonical ``BufferState``.
        """


class TimePolicy(RolloverPolicy):
    """Roll when wall clock has advanced ``interval_seconds`` since chunk start.

    The first ``append`` after a flush starts the timer; subsequent appends
    check elapsed time and trigger a rollover once the interval is crossed.
    """

    def __init__(self, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError(
                "TimePolicy.interval_seconds must be > 0; got " f"{interval_seconds}"
            )
        self.interval_seconds = interval_seconds

    def should_roll(self, state: BufferState) -> bool:
        elapsed = time.monotonic() - state.chunk_start_monotonic
        return elapsed >= self.interval_seconds


class SizePolicy(RolloverPolicy):
    """Roll when the buffered byte estimate reaches ``max_bytes``.

    The estimate is best-effort (``sys.getsizeof`` plus a one-level walk for
    common containers; pandas DataFrames are sized via
    ``memory_usage(deep=True).sum()``). Accuracy matters less than triggering
    *before* the pod OOMs — under-counting only widens the window between
    flushes, never causes a false rollover.
    """

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError(f"SizePolicy.max_bytes must be > 0; got {max_bytes}")
        self.max_bytes = max_bytes

    def should_roll(self, state: BufferState) -> bool:
        return state.buffered_bytes >= self.max_bytes


class CountPolicy(RolloverPolicy):
    """Roll when buffered record count reaches ``max_records``.

    "Records" defaults to ``len(batch)`` per ``append`` (so passing a list
    of 100 rows counts as 100); for batches that don't support ``len`` the
    count increments by 1 per append.
    """

    def __init__(self, max_records: int) -> None:
        if max_records <= 0:
            raise ValueError(f"CountPolicy.max_records must be > 0; got {max_records}")
        self.max_records = max_records

    def should_roll(self, state: BufferState) -> bool:
        return state.buffered_records >= self.max_records


class AnyOfPolicy(RolloverPolicy):
    """Composite — roll when *any* of the constituent policies trigger.

    First-hit-wins semantics. The constituent ``on_flush`` hooks are all
    called after every flush so size/count counters reset together.
    """

    def __init__(self, *policies: RolloverPolicy) -> None:
        if not policies:
            raise ValueError("AnyOfPolicy requires at least one policy")
        self.policies = policies

    def should_roll(self, state: BufferState) -> bool:
        return any(p.should_roll(state) for p in self.policies)

    def on_flush(self) -> None:
        for p in self.policies:
            p.on_flush()


# ---------------------------------------------------------------------------
# Size estimation
# ---------------------------------------------------------------------------


def _default_size_estimator(batch: Any) -> int:
    """Best-effort byte estimate for a single appended batch.

    Tries pandas' built-in deep memory accounting first (DataFrames and
    Series both expose ``memory_usage(deep=True)``). Falls back to
    ``sys.getsizeof`` plus a one-level walk for builtin containers
    (dict / list / tuple / set / frozenset) so we don't dramatically
    under-count a list of large dicts.

    Accuracy is intentionally best-effort. The writer uses this only to
    decide *when* to roll over, and an undercount merely widens the window
    between flushes. Callers that need precision can pass their own
    ``size_estimator`` to ``RollingFileWriter``.
    """
    memory_usage = getattr(batch, "memory_usage", None)
    if callable(memory_usage):
        try:
            usage = memory_usage(deep=True)
            return int(getattr(usage, "sum", lambda: usage)())
        except Exception:  # noqa: S110, BLE001 — best-effort sizer; pandas can raise on exotic dtypes
            pass

    base = sys.getsizeof(batch)
    if isinstance(batch, dict):
        return base + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in batch.items())
    if isinstance(batch, (list, tuple, set, frozenset)):
        return base + sum(sys.getsizeof(item) for item in batch)
    return base


def _record_count(batch: Any) -> int:
    """Best-effort record-count for a single appended batch.

    Uses ``len(batch)`` when defined; falls back to 1 otherwise (the batch
    is itself the single record).
    """
    try:
        return len(batch)
    except TypeError:
        return 1


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class RollingFileWriter(Generic[T]):
    """Roll over to a new chunk file when any configured policy fires.

    Buffers appended batches in memory; rolls the buffer to a single new
    chunk file once a configured rollover policy triggers (or on
    ``__aexit__`` / explicit ``flush()``).

    The simplest usage takes the three convenience kwargs:

    * ``chunk_interval_seconds`` — wall-clock interval (default 30 s).
    * ``max_buffer_bytes`` — buffer ceiling in bytes (default 50 MB).
    * ``max_buffer_records`` — buffer ceiling in records (default ``None`` —
      opt-in only).

    The writer composes these into an :class:`AnyOfPolicy` internally and
    rolls when *any* limit is hit. Set a kwarg to ``None`` to disable it.

    For advanced cases (e.g. fixed-size exports, custom hybrid logic) pass
    a ``rollover_policy`` directly — when set, the convenience kwargs are
    ignored.

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
            ``append`` triggers a rollover. Defaults to 30 s. Ignored when
            ``rollover_policy`` is set.
        max_buffer_bytes: Best-effort byte ceiling on the in-memory buffer.
            Defaults to 50 MB; set to ``None`` to disable. Ignored when
            ``rollover_policy`` is set.
        max_buffer_records: Record-count ceiling on the in-memory buffer.
            Defaults to ``None`` (opt-in). Ignored when ``rollover_policy``
            is set.
        rollover_policy: Advanced override. When set, the three convenience
            kwargs above are ignored and this policy decides every rollover.
            Use one of :class:`TimePolicy`, :class:`SizePolicy`,
            :class:`CountPolicy`, :class:`AnyOfPolicy`, or a custom
            :class:`RolloverPolicy` subclass.
        size_estimator: Optional ``(batch) -> int`` callback used by the
            built-in :class:`SizePolicy` (and any custom size-based policy
            that reads :pyattr:`BufferState.buffered_bytes`). Defaults to
            :func:`_default_size_estimator` which understands pandas
            DataFrames and common Python containers.
        on_chunk_complete: Optional callback ``(chunk_index, chunk_path) ->
            None`` (sync or async) invoked after every successful chunk
            flush. Useful for Temporal heartbeats — pass
            ``lambda idx, path: activity.heartbeat(f"chunk {idx}")``.
            Callback exceptions are logged but never abort the write loop.
        scoped_subdir_name: Optional explicit name for the writer-owned
            sub-directory. Defaults to ``f"rolling_{uuid.uuid4().hex[:8]}"``.
            Override when the directory name needs to be deterministic
            (e.g. for tests).
    """

    def __init__(
        self,
        base_path: str,
        extension: str,
        flush_fn: FlushFn[T],
        *,
        chunk_interval_seconds: float = _DEFAULT_CHUNK_INTERVAL_SECONDS,
        max_buffer_bytes: int | None = _DEFAULT_MAX_BUFFER_BYTES,
        max_buffer_records: int | None = None,
        rollover_policy: RolloverPolicy | None = None,
        size_estimator: SizeEstimatorFn | None = None,
        on_chunk_complete: ChunkCompleteFn | None = None,
        scoped_subdir_name: str | None = None,
    ) -> None:
        if not base_path:
            raise ValueError("base_path is required")
        if not extension.startswith("."):
            raise ValueError(f"extension must start with '.', got {extension!r}")

        self.output_dir = os.path.join(
            base_path,
            scoped_subdir_name or f"rolling_{uuid.uuid4().hex[:8]}",
        )
        SafeFileOps.makedirs(self.output_dir, exist_ok=True)

        self.extension = extension
        self.flush_fn = flush_fn
        self.on_chunk_complete = on_chunk_complete
        self._size_estimator: SizeEstimatorFn = (
            size_estimator if size_estimator is not None else _default_size_estimator
        )
        self.rollover_policy: RolloverPolicy = _resolve_rollover_policy(
            chunk_interval_seconds=chunk_interval_seconds,
            max_buffer_bytes=max_buffer_bytes,
            max_buffer_records=max_buffer_records,
            override=rollover_policy,
        )

        self._buffer: list[T] = []
        self._buffered_bytes = 0
        self._buffered_records = 0
        self._chunk_start_monotonic: float | None = None
        self._chunk_index = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RollingFileWriter[T]:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def append(self, batch: T) -> None:
        """Append a batch; trigger a rollover if any policy fires.

        Thread-safe within a single event loop via an internal asyncio Lock.
        """
        if self._closed:
            raise RuntimeError("Cannot append to a closed RollingFileWriter")

        async with self._lock:
            if self._chunk_start_monotonic is None:
                self._chunk_start_monotonic = time.monotonic()
            self._buffer.append(batch)

            try:
                self._buffered_bytes += int(self._size_estimator(batch))
            except Exception:
                # Defensive — a buggy estimator must not abort the write loop.
                logger.warning(
                    "RollingFileWriter size_estimator raised; treating batch "
                    "as zero bytes",
                    exc_info=True,
                )
            self._buffered_records += _record_count(batch)

            state = BufferState(
                chunk_start_monotonic=self._chunk_start_monotonic,
                buffered_batches=len(self._buffer),
                buffered_bytes=self._buffered_bytes,
                buffered_records=self._buffered_records,
            )
            if self.rollover_policy.should_roll(state):
                await self._flush_locked()

    async def flush(self) -> None:
        """Force-flush the current buffer to a new chunk file.

        Useful at activity boundaries when the caller wants a known
        checkpoint regardless of policy state.
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
                "RollingFileWriter flush failed",
                exc_info=True,
                extra={
                    "chunk_index": self._chunk_index,
                    "chunk_path": chunk_path,
                    "buffered_batches": len(self._buffer),
                    "buffered_bytes": self._buffered_bytes,
                    "buffered_records": self._buffered_records,
                },
            )
            raise

        completed_index = self._chunk_index
        self._chunk_index += 1
        self._buffer = []
        self._buffered_bytes = 0
        self._buffered_records = 0
        self._chunk_start_monotonic = None
        self.rollover_policy.on_flush()

        if self.on_chunk_complete is not None:
            try:
                cb_result = self.on_chunk_complete(completed_index, chunk_path)
                if inspect.isawaitable(cb_result):
                    await cb_result
            except Exception:
                # on_chunk_complete is best-effort (heartbeat etc.); never
                # let it abort the write loop.
                logger.warning(
                    "RollingFileWriter on_chunk_complete callback raised",
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
        return FileReference.from_local(self.output_dir)

    @property
    def chunk_count(self) -> int:
        """Number of chunk files written so far."""
        return self._chunk_index


def _resolve_rollover_policy(
    *,
    chunk_interval_seconds: float,
    max_buffer_bytes: int | None,
    max_buffer_records: int | None,
    override: RolloverPolicy | None,
) -> RolloverPolicy:
    """Build the writer's rollover policy from convenience kwargs.

    If ``override`` is set, the three threshold kwargs are ignored entirely
    and the override decides every rollover. Otherwise the kwargs compose
    into an :class:`AnyOfPolicy` of :class:`TimePolicy` plus whichever
    size/count limits are set.
    """
    if override is not None:
        return override

    # Validate convenience kwargs with their user-facing names so error
    # messages match what the caller actually passed (the policy classes'
    # internal field names would be confusing here).
    if chunk_interval_seconds <= 0:
        raise ValueError(
            f"chunk_interval_seconds must be > 0; got {chunk_interval_seconds}"
        )
    if max_buffer_bytes is not None and max_buffer_bytes <= 0:
        raise ValueError(
            f"max_buffer_bytes must be > 0 or None; got {max_buffer_bytes}"
        )
    if max_buffer_records is not None and max_buffer_records <= 0:
        raise ValueError(
            f"max_buffer_records must be > 0 or None; got {max_buffer_records}"
        )

    policies: list[RolloverPolicy] = [TimePolicy(chunk_interval_seconds)]
    if max_buffer_bytes is not None:
        policies.append(SizePolicy(max_buffer_bytes))
    if max_buffer_records is not None:
        policies.append(CountPolicy(max_buffer_records))
    return AnyOfPolicy(*policies) if len(policies) > 1 else policies[0]
