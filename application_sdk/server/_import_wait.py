"""Detect a worker whose import machinery is wedged (FND-5531).

Temporal runs each workflow activation on a worker thread and, when one runs
past its deadlock timeout (2s), raises into that thread with
``PyThreadState_SetAsyncExc`` (TMPRL1101). When that exception is delivered
inside ``importlib._bootstrap``, the import system can be left permanently
broken. Reproduced on CPython 3.11 to 3.14:

1. Thread B waits on module M's import lock while thread A imports M. The
   interrupt is queued on B while it is blocked.
2. A releases M and wakes B. B raises right after it takes M's internal
   ``wakeup`` lock and before it releases it, so ``wakeup`` stays held with no
   owner.
3. If M's import did not complete (on a cold, overloaded worker every attempt
   can be interrupted), threads keep contending on M. The next waiter blocks
   on ``wakeup`` forever.
4. That waiter was usually mid-import of an outer module P, so it holds P's
   import lock forever. Every later import of P, or of anything under P, then
   hangs in every thread, activities included.

The global import lock is never left held, so importing a fresh module still
works and a "can I import something?" probe stays green. What does reveal the
fault is a thread that waits on the same module import lock and never gets
it. CPython records exactly that in ``importlib._bootstrap._blocking_on`` for
its own import-deadlock detection, so :class:`ImportWaitWatch` samples that
table on each liveness check and reports a wait that has lasted too long.

``_blocking_on`` is private. It maps a thread ident to the ``_ModuleLock`` the
thread waits on (3.11) or to a list of them (3.12+). If it is missing or has
another shape, the watch fails open: it reports nothing and logs one warning.
"""

from __future__ import annotations

import importlib._bootstrap as _bootstrap
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class StuckImport:
    """A thread that has waited on one module import lock past the bound.

    Attributes:
        module: Name of the module whose import lock the thread waits on.
        thread: Name of the waiting thread.
        waited_seconds: How long the wait has been observed. It is measured
            from the first liveness check that saw it, so the real wait is at
            least this long.
    """

    module: str
    thread: str
    waited_seconds: float


@dataclass(frozen=True)
class _ImportWait:
    thread_ident: int
    thread_name: str
    module: str
    lock: object


@dataclass
class _Sighting:
    first_seen: float
    lock_ref: weakref.ref[object]
    reported: bool = False


def _module_lock_waits() -> list[_ImportWait] | None:
    """Return the module import locks that live threads are waiting on.

    Returns ``None`` when ``_blocking_on`` is missing or holds something this
    reader does not recognise, so the caller can fail open.
    """
    table = getattr(_bootstrap, "_blocking_on", None)
    get = getattr(table, "get", None)
    if not callable(get):
        return None
    waits: list[_ImportWait] = []
    for thread in threading.enumerate():
        ident = thread.ident
        if ident is None:
            continue
        entry = get(ident)
        if entry is None:
            continue
        # A copy: the waiting thread appends and removes on its own list.
        locks = list(entry) if isinstance(entry, list) else [entry]
        for lock in locks:
            module = getattr(lock, "name", None)
            if not isinstance(module, str):
                return None
            waits.append(_ImportWait(ident, thread.name, module, lock))
    return waits


class ImportWaitWatch:
    """Report a thread stuck waiting on a module import lock.

    Call :meth:`check` periodically (each ``/live`` request does). A wait is
    tracked by thread and lock from the first check that sees it; once it has
    lasted longer than ``max_wait_seconds``, :meth:`check` returns it. A wait
    that ends between two checks is forgotten, so a slow but progressing
    import never accumulates.
    """

    def __init__(
        self,
        max_wait_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialise the watch.

        Args:
            max_wait_seconds: How long one thread may wait on one module import
                lock before :meth:`check` reports it. Must be positive.
            clock: Monotonic clock in seconds. Injected by tests.
        """
        self._max_wait_seconds = max_wait_seconds
        self._clock = clock
        self._sightings: dict[tuple[int, int], _Sighting] = {}
        self._warned_unsupported = False

    def check(self) -> StuckImport | None:
        """Sample current import waits; return the oldest one past the bound."""
        waits = _module_lock_waits()
        if waits is None:
            self._sightings.clear()
            if not self._warned_unsupported:
                self._warned_unsupported = True
                logger.warning(
                    "Import-wait liveness check disabled: "
                    "importlib._bootstrap._blocking_on has an unrecognised shape "
                    "on this Python"
                )
            return None

        now = self._clock()
        sightings: dict[tuple[int, int], _Sighting] = {}
        oldest: tuple[float, _ImportWait, _Sighting] | None = None
        for wait in waits:
            key = (wait.thread_ident, id(wait.lock))
            sighting = self._sightings.get(key)
            # The id check alone could match a new lock reusing a freed one's
            # address, so confirm it is the same object.
            if sighting is None or sighting.lock_ref() is not wait.lock:
                sighting = _Sighting(first_seen=now, lock_ref=weakref.ref(wait.lock))
            sightings[key] = sighting
            waited = now - sighting.first_seen
            if waited > self._max_wait_seconds and (
                oldest is None or waited > oldest[0]
            ):
                oldest = (waited, wait, sighting)
        self._sightings = sightings

        if oldest is None:
            return None
        waited, wait, sighting = oldest
        if not sighting.reported:
            sighting.reported = True
            logger.error(
                "Thread %s has waited %.0fs on the import lock for module %s; "
                "the import system is wedged and /live will fail",
                wait.thread_name,
                waited,
                wait.module,
            )
        return StuckImport(
            module=wait.module, thread=wait.thread_name, waited_seconds=waited
        )
