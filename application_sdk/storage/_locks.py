"""Per-destination asyncio lock registries for the storage layer.

Two registries exist, one per layer, and they are deliberately separate:

* ``reference._MATERIALIZE_LOCKS`` — the dedupe mechanism. The second
  materialise caller waits, re-checks the now-complete file against its
  sidecar, and skips the duplicate download.
* ``chunked._TRANSFER_LOCKS`` — the exclusion mechanism. The chunked staging
  name (``.sdk-partial/{name}.part``) and its checkpoint are deterministic
  functions of the destination, so every caller of ``download_file_chunked``
  shares them; the lock serialises writers on the resource itself, whichever
  of the six public entry points they came through (CONNECT-1126 review).

Separate registries also mean the nesting is deadlock-free by construction:
``materialize_file_reference`` holds its guard for a destination and then
calls into the transfer layer, which acquires a *different* lock object for
the same path.

Neither lock is the reader-safety mechanism — that is the atomic
temp-then-``os.replace`` publish in the transfer layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import weakref
from collections.abc import AsyncIterator

from application_sdk._runtime.progress import current_progress_tracker


class PathLockRegistry:
    """Per-event-loop, per-realpath ``asyncio.Lock`` registry.

    Locks are registered per running event loop (an ``asyncio.Lock`` bound to
    one loop raises on a contended acquire from another), keyed by the
    destination's real path, and held only weakly — an entry evicts as soon
    as no caller is using it, and a loop's whole registry evicts with the
    loop.
    """

    def __init__(self, progress_label: str) -> None:
        self._progress_label = progress_label
        self._registries: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Lock]
        ] = weakref.WeakKeyDictionary()

    def lock(self, local_path: str) -> asyncio.Lock:
        """Return the lock for *local_path* on the running loop."""
        registry = self._registries.setdefault(
            asyncio.get_running_loop(), weakref.WeakValueDictionary()
        )
        key = os.path.realpath(local_path)
        lock = registry.get(key)
        if lock is None:
            lock = asyncio.Lock()
            registry[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def guard(self, local_path: str) -> AsyncIterator[None]:
        """Hold the per-destination lock, marking progress while queued.

        A waiter makes no transfer progress of its own, so a first download
        longer than the stall watchdog's no-progress budget would get the
        queued activity killed as stalled. Marking progress on a short
        interval while blocked keeps the waiter visibly alive.
        """
        from application_sdk.constants import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
            STORAGE_LOCK_WAIT_PROGRESS_SECONDS,
        )

        lock = self.lock(local_path)
        while True:
            try:
                await asyncio.wait_for(
                    lock.acquire(), timeout=STORAGE_LOCK_WAIT_PROGRESS_SECONDS
                )
                break
            except TimeoutError:
                current_progress_tracker().mark_progress(self._progress_label)
        try:
            yield
        finally:
            lock.release()
