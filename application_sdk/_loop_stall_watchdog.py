"""Exit the process when its event loop stops running for too long.

Imports nothing that starts a thread, so a process can fork safely right after
importing it.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator

LOOP_STALL_TIMEOUT_ENV = "ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS"
_REARM_CAP_SECONDS = 15.0
# faulthandler rejects a timeout of 0; a deadline already passed fires at once.
_MIN_TIMER_SECONDS = 0.001


class RunningHeartbeatTimeouts:
    """Heartbeat timeouts of the activities running in this process.

    Updated and read on the event loop thread only (activity interceptors and
    the watchdog both run there), so it takes no lock.
    """

    def __init__(self) -> None:
        self._counts: Counter[float] = Counter()
        self.on_change: Callable[[], None] | None = None

    def longest(self) -> float:
        """The longest heartbeat timeout among running activities, else 0."""
        return max(self._counts, default=0.0)

    @contextlib.contextmanager
    def track(self, heartbeat_timeout_seconds: float | None) -> Iterator[None]:
        """Count an activity as running for the duration of the block.

        An activity without a heartbeat timeout is not counted: Temporal never
        gives up on it for missed heartbeats, so it has nothing to lose.
        """
        if not heartbeat_timeout_seconds or heartbeat_timeout_seconds <= 0:
            yield
            return
        self._counts[heartbeat_timeout_seconds] += 1
        self._notify()
        try:
            yield
        finally:
            self._counts[heartbeat_timeout_seconds] -= 1
            if self._counts[heartbeat_timeout_seconds] <= 0:
                del self._counts[heartbeat_timeout_seconds]
            self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()


running_heartbeat_timeouts = RunningHeartbeatTimeouts()


class LoopStallWatchdog:
    """Exits the process when its event loop stops running for too long.

    Shutdown signals are handled on the loop (``loop.add_signal_handler``), so
    a loop stuck in a lock wait or an untimed call never sees SIGTERM. The
    Temporal core keeps polling on its own threads: the process goes on taking
    workflow and activity tasks it cannot run, each one times out, and this
    lasts until SIGKILL at the end of the pod's grace period. The kubelet stops
    running liveness probes once a pod is terminating, so only the process
    itself can end that.

    A task on the loop re-arms ``faulthandler.dump_traceback_later`` well
    inside the timeout. Its timer runs on a C thread that needs neither the
    loop nor the GIL, so when the loop stops re-arming it, the timer writes
    every thread's stack to stderr (the evidence of what blocked) and exits
    with code 1. faulthandler keeps one such timer per process, so nothing else
    in the SDK may use ``dump_traceback_later``.

    The timeout is ``max(timeout_seconds, longest heartbeat timeout among the
    running activities)``. A loop silent for longer than a running activity's
    heartbeat timeout has already lost that activity to Temporal, so waiting
    that long costs nothing, while a shorter fixed bound could end work that
    would have finished. An idle worker uses ``timeout_seconds``.

    The caller decides how long it runs. In the worker that is from worker
    start through the drain, where the loop keeps ticking; once the worker has
    drained on a shutdown signal, ``_PostDrainExitArm`` stops it and the
    post-drain exit deadline bounds the rest, so a normal exit never trips it.
    """

    def __init__(
        self,
        timeout_seconds: float,
        *,
        running: RunningHeartbeatTimeouts | None = None,
        on_timeout_change: Callable[[float], None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._running = running if running is not None else running_heartbeat_timeouts
        self._on_timeout_change = on_timeout_change
        self._effective_seconds = timeout_seconds
        self._wake = asyncio.Event()
        self._lock = threading.Lock()
        self._armed = False
        self._paused_for_fork = False
        self._stopped = False
        self._deadline = 0.0
        # The loop holds tasks weakly; this reference keeps the re-arm alive.
        self._task: asyncio.Future[None] | None = None

    @classmethod
    def from_settings(
        cls, *, on_timeout_change: Callable[[float], None] | None = None
    ) -> LoopStallWatchdog:
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: worker startup only
            load_worker_loop_stall_timeout_seconds,
        )

        return cls(
            load_worker_loop_stall_timeout_seconds(),
            on_timeout_change=on_timeout_change,
        )

    def effective_timeout_seconds(self) -> float:
        return max(self.timeout_seconds, self._running.longest())

    def start(self) -> None:
        """Start re-arming from the running loop. A timeout of 0 disables it."""
        if self.timeout_seconds <= 0:
            return
        self._register_fork_hooks()
        self._running.on_change = self._on_running_change
        self._task = asyncio.ensure_future(self._rearm_until_stopped())

    def _on_running_change(self) -> None:
        # Re-arm now: the activity that just started may block the loop before
        # the re-arm task runs again. Then wake that task, so a timeout that
        # just shrank is not followed by a sleep sized for the longer one.
        self._arm()
        self._wake.set()

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

    def stop(self) -> None:
        """Cancel the timer for good. Idempotent and safe from any thread."""
        if self._running.on_change == self._on_running_change:
            self._running.on_change = None
        with self._lock:
            self._stopped = True
            if self._armed:
                faulthandler.cancel_dump_traceback_later()
                self._armed = False

    def _arm(self) -> float | None:
        """Re-arm for the effective timeout; ``None`` once stopped."""
        timeout = self.effective_timeout_seconds()
        with self._lock:
            if self._stopped:
                return None
            self._deadline = time.monotonic() + timeout
            self._set_timer(timeout)
        if timeout != self._effective_seconds:
            self._effective_seconds = timeout
            if self._on_timeout_change is not None:
                self._on_timeout_change(timeout)
        return timeout

    def _set_timer(self, seconds: float) -> None:
        # A file descriptor, not sys.stderr: the timer writes from C after the
        # Python-level stream may have been replaced or wrapped.
        faulthandler.dump_traceback_later(seconds, repeat=False, file=2, exit=True)
        self._armed = True

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
            while (timeout := self._arm()) is not None:
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), rearm_interval_seconds(timeout)
                    )
        finally:
            self.stop()


def rearm_interval_seconds(timeout_seconds: float) -> float:
    """Several re-arms per timeout, so one late tick never trips it."""
    return min(timeout_seconds / 4, _REARM_CAP_SECONDS)
