"""The one background event loop that runs every periodic observability flush.

Before this module each sink started its own: the log adapter a
``new_event_loop`` + ``run_forever`` thread, the metrics and traces adapters a
task on whatever loop was running or else an ``asyncio.run`` thread, the sizing
sink a hand-made loop on a thread, and the Segment client a dedicated thread and
loop it waited up to 5 s to come up. Up to five loops, each with its own
cadence and its own (or no) shutdown, so no single bound covered the final
flushes at exit.

Now one daemon thread (``atlan-observability-loop``) runs one loop, started on
first use. Long-lived work is started with :meth:`ObservabilityLoop.start`,
which returns a :class:`BackgroundTask` handle. :meth:`ObservabilityLoop.shutdown`
runs once at interpreter exit (``atexit``) and stops everything within one
bound, ``OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS``:

1. every started task is cancelled once; the periodic flushes make their final
   flush in their ``CancelledError`` handler, and the Segment worker sends its
   last batch and closes its HTTP client;
2. every other task on the loop (a batch flush ``add_record`` started there) is
   awaited, never cancelled, because cancelling one deletes its half-written
   file and loses the batch;
3. the loop stops and the thread is joined with what is left of the bound.

Two pieces of background work deliberately stay off this loop. The console
writer (``console_sink``) is a plain thread because its whole job is a blocking
``write()`` to stdout; on this loop a stalled pipe would stall every flush. The
OTLP log exporter's batch thread belongs to the OpenTelemetry SDK.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import os
import threading
import time
from collections.abc import Coroutine
from typing import Any

from application_sdk.constants import OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS

THREAD_NAME = "atlan-observability-loop"


class BackgroundTask:
    """Handle to a long-lived coroutine running on the observability loop.

    Thread-safe. :meth:`cancel` may be called from any thread, any number of
    times: the task is cancelled at most once, on the loop's own thread, so a
    second cancel can never interrupt the final flush the first one started.
    """

    def __init__(self, name: str, loop: asyncio.AbstractEventLoop) -> None:
        self.name = name
        self.loop = loop
        self._task: asyncio.Task[Any] | None = None
        # Set by the task's first step. Until then a cancel is deferred: a task
        # cancelled before its first step never enters the coroutine, so its
        # ``CancelledError`` handler (the final flush) would never run.
        self._started = False
        self._cancel_requested = False
        self._cancel_sent = False
        self._done = threading.Event()

    def done(self) -> bool:
        """Whether the coroutine has finished, or will never run."""
        return self._done.is_set()

    def wait(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for the coroutine to finish.

        Returns ``True`` if it finished. Never call from the loop's thread.
        """
        return self._done.wait(timeout)

    def cancel(self) -> None:
        """Ask the coroutine to stop. Returns at once; use :meth:`wait` to wait."""
        self._cancel_requested = True
        try:
            self.loop.call_soon_threadsafe(self._cancel_on_loop)
        except RuntimeError:
            # The loop is closed, so the coroutine can never run again.
            self._done.set()

    async def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """The task body: mark the start, then run *coro*."""
        self._started = True
        return await coro

    def _attach(self, task: asyncio.Task[Any]) -> None:
        """Bind the running task. Runs on the loop's thread."""
        self._task = task
        task.add_done_callback(lambda _t: self._done.set())
        if self._cancel_requested:
            self._cancel_on_loop()

    def _cancel_on_loop(self) -> None:
        """Cancel the task once. Runs on the loop's thread only."""
        if self._task is None or self._cancel_sent:
            return
        if not self._started:
            # Retry after the task's first step, which is already scheduled.
            self.loop.call_soon(self._cancel_on_loop)
            return
        self._cancel_sent = True
        self._task.cancel()


class ObservabilityLoop:
    """A single daemon thread running a single asyncio event loop.

    Started lazily on the first :meth:`start` or :meth:`submit`, so importing
    the SDK does not start a thread. Fork-aware: a child process, which
    inherits this object but not the thread, starts its own on first use.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_state()

    def _init_state(self) -> None:
        self._pid = os.getpid()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: set[BackgroundTask] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether :meth:`shutdown` has run. A closed loop starts nothing."""
        return self._closed

    def in_loop_thread(self) -> bool:
        """Whether the caller is running on the observability loop's thread."""
        thread = self._thread
        return thread is not None and thread.ident == threading.get_ident()

    def _ensure_started_locked(self) -> asyncio.AbstractEventLoop | None:
        """The running loop, starting it if needed; ``None`` once shut down.

        Caller holds ``_lock``.
        """
        if self._pid != os.getpid():
            self._init_state()
        if self._closed:
            return None
        if self._loop is not None and self._thread is not None:
            if self._thread.is_alive():
                return self._loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=self._run, args=(loop,), name=THREAD_NAME, daemon=True
        )
        thread.start()
        self._loop, self._thread = loop, thread
        return loop

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    def submit(
        self, coro: Coroutine[Any, Any, Any]
    ) -> concurrent.futures.Future[Any] | None:
        """Schedule a one-off *coro* on the loop from any thread.

        Returns the ``concurrent.futures.Future`` for its result, or ``None``
        (and closes *coro*) when the loop has been shut down.
        """
        with self._lock:
            loop = self._ensure_started_locked()
        if loop is None:
            coro.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            # The loop closed between the lock and the schedule.
            coro.close()
            return None

    def start(
        self, coro: Coroutine[Any, Any, Any], *, name: str
    ) -> BackgroundTask | None:
        """Run the long-lived *coro* on the loop until it ends or is cancelled.

        *coro* must handle ``asyncio.CancelledError`` as its signal to finish
        up (final flush, final batch): :meth:`shutdown` cancels it once and
        waits for it within the shutdown bound.

        Returns its handle, or ``None`` (and closes *coro*) when the loop has
        been shut down.
        """
        with self._lock:
            loop = self._ensure_started_locked()
            if loop is None:
                coro.close()
                return None
            handle = BackgroundTask(name, loop)
            self._tasks = {t for t in self._tasks if not t.done()}
            self._tasks.add(handle)

        def _spawn() -> None:
            handle._attach(loop.create_task(handle._run(coro), name=name))

        try:
            loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            coro.close()
            handle._done.set()
            with self._lock:
                self._tasks.discard(handle)
            return None
        return handle

    def shutdown(self, timeout: float = OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        """Stop every task and the loop within *timeout* seconds. Idempotent.

        Returns ``True`` if everything finished inside the bound. Past it, the
        loop is stopped anyway and whatever was still running is abandoned:
        the thread is a daemon, so it cannot hold the process open.
        """
        with self._lock:
            if self._pid != os.getpid():
                self._init_state()
            if self._closed:
                return True
            self._closed = True
            loop, thread = self._loop, self._thread
            tasks = list(self._tasks)
        if loop is None or thread is None or not thread.is_alive():
            return True
        if self.in_loop_thread():
            # Cannot wait on our own thread; stop without waiting.
            loop.call_soon(loop.stop)
            return False

        deadline = time.monotonic() + max(0.0, timeout)
        clean = True
        try:
            drain = asyncio.run_coroutine_threadsafe(_drain(tasks), loop)
        except RuntimeError:
            return True  # Already closed.
        try:
            drain.result(timeout=max(0.0, deadline - time.monotonic()))
        except TimeoutError:
            clean = False
            drain.cancel()
            logging.warning(
                "Observability background loop did not finish its final flushes "
                "within %ss; abandoning the rest",
                timeout,
            )
        # conformance: ignore[E004] shutdown path; a failed drain must not block exit
        except Exception:
            clean = False
            logging.warning("Error stopping observability tasks", exc_info=True)
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return clean  # Already closed.
        thread.join(max(0.0, deadline - time.monotonic()))
        return clean and not thread.is_alive()

    def _reset_for_testing(self, timeout: float = 5.0) -> None:
        """Shut down (bounded by *timeout*) and allow a fresh start.

        Test-only: production shuts down once, at exit.
        """
        self.shutdown(timeout)
        with self._lock:
            self._init_state()


async def _drain(tasks: list[BackgroundTask]) -> None:
    """Cancel each started task once, then wait for every task on the loop.

    Runs on the loop. Tasks it did not start (a batch flush ``add_record``
    began on this loop) are awaited, not cancelled. Re-checks until none are
    left, since a final flush can log and so start another batch flush.
    """
    for handle in tasks:
        handle._cancel_requested = True
        handle._cancel_on_loop()
    current = asyncio.current_task()
    while True:
        pending = [t for t in asyncio.all_tasks() if t is not current]
        if not pending:
            return
        results = await asyncio.gather(*pending, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logging.warning(
                    "Observability background task failed at shutdown",
                    exc_info=(type(result), result, result.__traceback__),
                )


#: The process-wide observability loop.
observability_loop = ObservabilityLoop()

# The background thread is a daemon, which interpreter exit stops where it
# stands; give the final flushes one bounded window first. atexit runs
# handlers in reverse registration order, so handlers registered after this
# import (none of the sinks need one any more) run before it.
atexit.register(observability_loop.shutdown)
