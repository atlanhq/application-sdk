"""One in-flight byte budget shared by the SDK's file transfers.

The storage layer's concurrency limits nest: refs × files per ref × range GETs
per file, each holding a whole chunk in memory. None of them knows about the
others, so their product — not any one of them — is what a worker's memory
has to hold, and four directory refs of large files came to about 1 GiB
against a median 3 GiB container limit. An OOM kill then surfaces as a
timeout or an untyped failure, never as a resource error.

:func:`transfer_budget` is the one bound on that product. Each transfer
reserves the bytes it will hold in memory before it holds them:

* a range GET in ``chunked._fetch_chunk``, for its range, until the bytes are
  written to disk;
* a streaming GET in ``ops.download_file``, for its stream buffer, while it
  streams;
* a multipart upload in ``ops._upload_once``, for its part buffers, while its
  writer is open.

``ops.upload_bytes`` streams through ``_upload_once`` too, so it reserves like
a file upload. Whole-object reads and small writes do not reserve —
``CloudStore.get_bytes`` and the small-object ``_get_bytes`` / ``_put``
helpers (under ``put_json`` and the sidecar writes) hold what they move outside
the budget. ``CloudStore``'s other transfers run on the primitives above and
reserve like any other.

No holder ever asks for a second reservation, so the budget cannot deadlock
against itself. A request larger than the whole budget is clamped to it: it
waits until the budget is empty and then runs alone, rather than never.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import weakref
from collections.abc import AsyncIterator
from types import ModuleType

from application_sdk._install import lazy_getattr, worker_only_missing
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.storage import _settings

_MIB = 1024 * 1024

#: Share of the container memory limit the budget takes by default. The rest
#: is the app's own working set — a transfer is rarely the only thing a worker
#: is doing.
_LIMIT_FRACTION = 0.25

#: Floor: one 16 MiB range GET per in-flight file of a four-way fan-out, plus
#: room for the uploads beside them. Below this the budget serialises
#: transfers without saving memory that matters.
_MIN_BUDGET_BYTES = 128 * _MIB

#: When no container limit is known (local development, a test run).
_UNKNOWN_LIMIT_BUDGET_BYTES = 1024 * _MIB


def _cgroup() -> ModuleType | None:
    """The worker's cgroup readings, or ``None`` on an api-only install.

    ``application_sdk.observability.cgroup`` is worker code; the API host ships
    this module without it and sizes its budget as for an unknown limit.
    """
    try:
        from application_sdk.observability import (  # noqa: PLC0415 — worker-only; absent on the API host
            cgroup,
        )
    except ModuleNotFoundError as exc:
        if not worker_only_missing(exc):
            raise
        return None
    return cgroup


#: ``cgroup`` was imported here at module level before this module shipped on
#: the API host; on the worker it stays an attribute of this module.
_LAZY: dict[str, tuple[str, str]] = {
    "cgroup": ("application_sdk.observability", "cgroup"),
}


def __getattr__(name: str) -> ModuleType:
    if name == "cgroup":
        _cgroup()  # imports the submodule, so its package has it as an attribute
    return lazy_getattr(__name__, name, _LAZY, globals())


def budget_bytes() -> int:
    """Return the configured budget: the override, else a share of the limit."""
    override = _settings.transfer_memory_budget_bytes()
    if override > 0:
        return override
    cgroup = _cgroup()
    limit = None if cgroup is None else cgroup.memory_limit_bytes()
    if limit is None:
        return _UNKNOWN_LIMIT_BUDGET_BYTES
    return max(_MIN_BUDGET_BYTES, int(limit * _LIMIT_FRACTION))


class ByteBudget:
    """A first-come, first-served weighted semaphore over bytes.

    First-come, first-served so a large request — a multipart upload's part
    buffers — is not starved by a stream of small range GETs that would each
    fit around it. Bound to no event loop of its own; :func:`transfer_budget`
    keeps one per loop.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"budget capacity must be positive, got {capacity}")
        self.capacity = capacity
        self._used = 0
        #: High-water mark of reserved bytes, for tests and benchmarks.
        self.peak = 0
        self._waiters: collections.deque[tuple[int, asyncio.Future[None]]] = (
            collections.deque()
        )

    @property
    def used(self) -> int:
        """Bytes currently reserved."""
        return self._used

    @contextlib.asynccontextmanager
    async def reserve(self, nbytes: int) -> AsyncIterator[int]:
        """Hold *nbytes* of the budget for the body; yield the amount held.

        Clamped to the capacity, so a request larger than the whole budget
        runs alone instead of waiting forever. While queued, progress is
        marked on the same interval as a storage lock wait: a waiter moves no
        bytes of its own, and the stall watchdog must not kill it for queuing
        behind another activity's transfer.
        """
        held = min(max(nbytes, 0), self.capacity)
        if held:
            await self._acquire(held)
        try:
            yield held
        finally:
            if held:
                self._release(held)

    def _grant(self, nbytes: int) -> None:
        self._used += nbytes
        if self._used > self.peak:
            self.peak = self._used

    async def _acquire(self, nbytes: int) -> None:
        if not self._waiters and self._used + nbytes <= self.capacity:
            self._grant(nbytes)
            return
        lock_wait_seconds = _settings.lock_wait_progress_seconds()

        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (nbytes, fut)
        self._waiters.append(entry)
        # Waiters ahead may all be cancelled ones nobody has popped yet.
        self._wake()
        try:
            while True:
                # asyncio.wait, not wait_for: a timeout must leave the future
                # queued, not cancel it.
                done, _ = await asyncio.wait({fut}, timeout=lock_wait_seconds)
                if done:
                    return
                current_progress_tracker().mark_progress("storage.budget_wait")
        except BaseException:
            if fut.done() and not fut.cancelled():
                # Granted, then cancelled before resuming: hand the bytes on.
                self._release(nbytes)
            else:
                fut.cancel()
                with contextlib.suppress(ValueError):
                    self._waiters.remove(entry)
                # A cancelled head may have been all that blocked the next.
                self._wake()
            raise

    def _release(self, nbytes: int) -> None:
        self._used -= nbytes
        self._wake()

    def _wake(self) -> None:
        while self._waiters:
            nbytes, fut = self._waiters[0]
            if fut.done():
                self._waiters.popleft()
                continue
            if self._used + nbytes > self.capacity:
                return
            self._waiters.popleft()
            self._grant(nbytes)
            fut.set_result(None)


_BUDGETS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, ByteBudget] = (
    weakref.WeakKeyDictionary()
)


def transfer_budget() -> ByteBudget:
    """Return the running loop's transfer budget, creating it on first use.

    A worker runs one event loop, so this is the process-wide budget. Keyed by
    loop for the same reason ``_locks.PathLockRegistry`` is: its waiters are
    futures of the loop that queued them.
    """
    loop = asyncio.get_running_loop()
    budget = _BUDGETS.get(loop)
    if budget is None:
        budget = ByteBudget(budget_bytes())
        _BUDGETS[loop] = budget
    return budget
