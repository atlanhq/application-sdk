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
    has been silent for at least the bound when the timer fires, unless the
    exit deadline cut the arm short."""
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
    already; with ``exit_by_seconds`` it still ends the process that long
    after the signal at the latest, however late in the drain it starts. The
    timer runs on a C thread that needs neither the loop nor the
    GIL: when it runs out it writes every thread's stack to stderr (the
    evidence of what blocked) and exits with code 1. faulthandler keeps one
    such timer per process, so nothing else in the SDK may use
    ``dump_traceback_later``.

    While the loop runs, the shutdown handler's re-arm task keeps pushing the
    deadline back through the drain, never past ``exit_by_seconds`` after the
    signal. Once the worker has drained, ``_PostDrainExitArm`` stops it and the
    post-drain exit deadline bounds the rest, so a normal exit never trips it.
    The shutdown budget sets the drain's graceful timeout to end before
    ``exit_by_seconds``, so the cap ends only a drain that has overrun it, such
    as an activity still running after it was cancelled. That exit writes the
    same stack dump and code 1 even though the loop may still be running, so
    the last re-arm before the deadline calls ``on_exit_deadline`` from the
    running loop, letting the caller log that a dump which follows is the
    deadline and not a stall.
    """

    def __init__(
        self,
        timeout_seconds: float,
        exit_by_seconds: float | None = None,
        *,
        on_exit_deadline: Callable[[float], None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        # How long after the signal the exit must land; None leaves it to the
        # bound alone (no grace period declared).
        self.exit_by_seconds = exit_by_seconds
        self._exit_by: float | None = None
        # Told, once, the seconds left when only the exit deadline can still end
        # the process. A callback rather than a log call: this module must not
        # import the SDK logger (see the module docstring).
        self._on_exit_deadline = on_exit_deadline
        self._exit_deadline_reported = False
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
        now = time.monotonic()
        if self.exit_by_seconds is not None:
            self._exit_by = now + self.exit_by_seconds
        seconds = self._timer_from(now)
        self._deadline = now + seconds
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
            now = time.monotonic()
            seconds = self._timer_from(now)
            self._deadline = now + seconds
            self._set_timer(seconds)
            final = self._is_final_arm(now)
        # Outside the lock, and only here on the loop: never from the signal
        # handler, which may have interrupted code holding the logging lock.
        if final and not self._exit_deadline_reported:
            self._exit_deadline_reported = True
            if self._on_exit_deadline is not None:
                self._on_exit_deadline(seconds)
        return True

    def _is_final_arm(self, now: float) -> bool:
        """Whether the exit deadline comes before the next re-arm would.

        From then on the deadline, not a stall, ends the process unless the
        drain finishes first. Only the loop re-arms, so reaching this means the
        loop is still running. A timer merely shortened by the deadline is not
        enough: on a short grace that is every re-arm of a normal drain.
        """
        if self._exit_by is None:
            return False
        return self._exit_by - now <= rearm_interval_seconds(self.timeout_seconds)

    def _timer_from(self, now: float) -> float:
        seconds = timer_seconds(self.timeout_seconds)
        if self._exit_by is None:
            return seconds
        return max(min(seconds, self._exit_by - now), _MIN_TIMER_SECONDS)

    def _set_timer(self, seconds: float) -> None:
        # A file descriptor, not sys.stderr: the timer writes from C after the
        # Python-level stream may have been replaced or wrapped.
        faulthandler.dump_traceback_later(seconds, repeat=False, file=2, exit=True)
        self._armed = True

    def _register_fork_hooks(self) -> None:
        # A forked child inherits the armed timer but not its thread, and an
        # interpreter exit in that child then waits on the thread forever.
        # Disarm across the fork; the parent re-arms after it. faulthandler
        # keeps one timer per process, so the hooks follow the latest watchdog
        # and are registered once: os.register_at_fork cannot unregister. Only
        # the latest can be armed, since a process runs one worker at a time.
        global _fork_target, _fork_hooks_registered
        _fork_target = self
        if _fork_hooks_registered or not hasattr(os, "register_at_fork"):
            return
        os.register_at_fork(
            before=_pause_for_fork,
            after_in_parent=_resume_after_fork,
            after_in_child=_release_after_fork,
        )
        _fork_hooks_registered = True

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


_fork_target: LoopStallWatchdog | None = None
_fork_hooks_registered = False
# The watchdog paused by the current fork, so both sides release the lock the
# same one took even if a new watchdog is installed in between.
_forking: LoopStallWatchdog | None = None


def _pause_for_fork() -> None:
    global _forking
    _forking = _fork_target
    if _forking is not None:
        _forking._pause_for_fork()


def _resume_after_fork() -> None:
    global _forking
    watchdog, _forking = _forking, None
    if watchdog is not None:
        watchdog._resume_after_fork()


def _release_after_fork() -> None:
    global _forking
    watchdog, _forking = _forking, None
    if watchdog is not None:
        watchdog._release_after_fork()
