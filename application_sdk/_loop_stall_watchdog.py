"""Exit a shutting-down process whose event loop has stopped running.

Imports nothing that starts a thread, so a process can fork safely right after
importing it.
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable
from types import FrameType
from typing import Any

LOOP_STALL_TIMEOUT_ENV = "ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS"
_REARM_CAP_SECONDS = 15.0
# faulthandler rejects a timeout of 0; a deadline already passed fires at once.
_MIN_TIMER_SECONDS = 0.001

_SignalHandler = Callable[[int, FrameType | None], Any]


def rearm_interval_seconds(timeout_seconds: float) -> float:
    """Several re-arms per bound, so one late tick never trips it."""
    return min(timeout_seconds / 4, _REARM_CAP_SECONDS)


def timer_seconds(timeout_seconds: float) -> float:
    """How long each arm lasts: the bound plus one re-arm interval, so the loop
    has been silent for at least the bound whenever the timer fires."""
    return timeout_seconds + rearm_interval_seconds(timeout_seconds)


def largest_bound_within(seconds: float) -> float:
    """The largest bound whose timer runs out within ``seconds``."""
    # timer_seconds(t) is 5t/4 up to t = 60, then t + 15.
    if seconds >= 5 * _REARM_CAP_SECONDS:
        return seconds - _REARM_CAP_SECONDS
    return seconds * 4 / 5


class LoopStallWatchdog:
    """Exits a shutting-down process whose event loop has stopped running.

    Shutdown signals are handled on the loop (``loop.add_signal_handler``), so
    a loop stuck in a lock wait or an untimed call never runs the graceful
    shutdown. The Temporal core keeps polling on its own threads: the process
    goes on taking workflow and activity tasks it cannot run until SIGKILL at
    the end of the pod's grace period, and the kubelet stops running liveness
    probes once a pod is terminating. Before shutdown a frozen loop is the
    liveness probe's job (the health server runs on the same loop), so this is
    armed only by a shutdown signal and never limits a running worker.

    The first SIGTERM or SIGINT arms ``faulthandler.dump_traceback_later``
    from a process-level ``signal.signal`` handler. The interpreter runs that
    handler only when the main thread is back in Python bytecode. A lock wait
    and a blocking system call that PEP 475 retries both return there, so a
    loop already frozen in one of them when the signal arrives is covered. A
    native call that keeps waiting inside C delays the arming until it
    returns, whether or not it released the GIL, and one that never returns
    never arms it: such a freeze is left to SIGKILL. A freeze that starts
    after the signal is always covered, because the timer is then armed
    already. The timer runs on a C thread that needs neither the loop nor the
    GIL: when it runs out it writes every thread's stack to stderr (the
    evidence of what blocked) and exits with code 1. faulthandler keeps one
    such timer per process, so nothing else in the SDK may use
    ``dump_traceback_later``.

    While the loop runs, the shutdown handler's re-arm task keeps pushing the
    deadline back, through the whole drain. Once the worker has drained,
    ``_PostDrainExitArm`` stops it and the post-drain exit deadline bounds the
    rest, so a normal exit never trips it.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._armed = False
        self._paused_for_fork = False
        self._stopped = False
        self._shutting_down = False
        self._deadline = 0.0
        # The loop holds tasks weakly; this reference keeps the re-arm alive.
        self._task: asyncio.Future[None] | None = None

    def install(self, signals: Iterable[signal.Signals]) -> None:
        """Arm at the first of ``signals``, ahead of their current handlers.

        Call on the main thread after the loop's own handlers are in place:
        asyncio's handler is a Python-level no-op that learns of the signal
        through its wakeup fd, so chaining in front of it leaves the graceful
        shutdown unchanged.
        """
        if self.timeout_seconds <= 0:
            return
        self._register_fork_hooks()
        for sig in signals:
            try:
                previous = signal.getsignal(sig)
                signal.signal(sig, self._chained(previous))
            # conformance: ignore[E002] off the main thread no process-level handler can be set; the loop's shutdown handler still arms it via start_rearming
            except ValueError:
                return

    def _chained(self, previous: Any) -> _SignalHandler:
        def _handler(signum: int, frame: FrameType | None) -> None:
            self.on_shutdown_signal()
            if callable(previous):
                previous(signum, frame)

        return _handler

    def on_shutdown_signal(self) -> None:
        """Arm the timer, once.

        Runs inside a signal handler on the main thread, which may have been
        interrupted while holding the lock, so it takes no lock and does
        nothing after the first call. Until shutdown nothing else arms the
        timer, so the first call has it to itself.
        """
        if self._shutting_down or self._stopped or self.timeout_seconds <= 0:
            return
        self._shutting_down = True
        seconds = timer_seconds(self.timeout_seconds)
        self._deadline = time.monotonic() + seconds
        self._set_timer(seconds)

    def start_rearming(self) -> None:
        """Keep the timer pushed back while the loop runs. Call from the loop's
        shutdown handler; it also arms where no process-level handler could be
        installed."""
        if self.timeout_seconds <= 0 or self._task is not None:
            return
        self.on_shutdown_signal()
        self._task = asyncio.ensure_future(self._rearm_until_stopped())

    def stop(self) -> None:
        """Cancel the timer for good. Idempotent and safe from any thread."""
        with self._lock:
            self._stopped = True
            if self._armed:
                faulthandler.cancel_dump_traceback_later()
                self._armed = False

    def _arm(self) -> bool:
        with self._lock:
            if self._stopped:
                return False
            seconds = timer_seconds(self.timeout_seconds)
            self._deadline = time.monotonic() + seconds
            self._set_timer(seconds)
            return True

    def _set_timer(self, seconds: float) -> None:
        # A file descriptor, not sys.stderr: the timer writes from C after the
        # Python-level stream may have been replaced or wrapped.
        faulthandler.dump_traceback_later(seconds, repeat=False, file=2, exit=True)
        self._armed = True

    def _register_fork_hooks(self) -> None:
        if not hasattr(os, "register_at_fork"):
            return
        # A forked child inherits the armed timer but not its thread, and an
        # interpreter exit in that child then waits on the thread forever.
        # Disarm across the fork; the parent re-arms after it.
        os.register_at_fork(
            before=self._pause_for_fork,
            after_in_parent=self._resume_after_fork,
            after_in_child=self._release_after_fork,
        )

    # The lock is held from before the fork until after it, so the loop cannot
    # re-arm in between and hand the child a live timer.
    def _pause_for_fork(self) -> None:
        self._lock.acquire()
        if self._armed:
            faulthandler.cancel_dump_traceback_later()
            self._armed = False
            self._paused_for_fork = True

    def _resume_after_fork(self) -> None:
        try:
            if self._paused_for_fork and not self._stopped:
                # Keep the loop's deadline: a thread that forks over and over
                # must not postpone the exit of a loop that has stopped.
                remaining = self._deadline - time.monotonic()
                self._set_timer(max(remaining, _MIN_TIMER_SECONDS))
            self._paused_for_fork = False
        finally:
            self._lock.release()

    def _release_after_fork(self) -> None:
        self._paused_for_fork = False
        self._lock.release()

    async def _rearm_until_stopped(self) -> None:
        try:
            while self._arm():
                await asyncio.sleep(rearm_interval_seconds(self.timeout_seconds))
        finally:
            self.stop()
