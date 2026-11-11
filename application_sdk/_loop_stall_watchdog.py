"""Exit the process when its event loop stops running for too long.

Imports nothing that starts a thread, so a process can fork safely right after
importing it.
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import threading
import time

LOOP_STALL_TIMEOUT_ENV = "ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS"
# Several re-arms fit inside one timeout, so a single late tick never trips it.
_REARM_CAP_SECONDS = 15.0
# faulthandler rejects a timeout of 0; a deadline already passed fires at once.
_MIN_TIMER_SECONDS = 0.001


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

    The caller decides how long it runs. In the worker that is from worker
    start through the drain, where the loop keeps ticking; once the worker has
    drained on a shutdown signal, ``_PostDrainExitArm`` stops it and the
    post-drain exit deadline bounds the rest, so a normal exit never trips it.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._rearm_seconds = min(timeout_seconds / 4, _REARM_CAP_SECONDS)
        self._lock = threading.Lock()
        self._armed = False
        self._paused_for_fork = False
        self._stopped = False
        self._deadline = 0.0
        # The loop holds tasks weakly; this reference keeps the re-arm alive.
        self._task: asyncio.Future[None] | None = None

    @classmethod
    def from_settings(cls) -> LoopStallWatchdog:
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: worker startup only
            load_worker_loop_stall_timeout_seconds,
        )

        return cls(load_worker_loop_stall_timeout_seconds())

    def start(self) -> None:
        """Start re-arming from the running loop. A timeout of 0 disables it."""
        if self.timeout_seconds <= 0:
            return
        self._register_fork_hooks()
        self._task = asyncio.ensure_future(self._rearm_until_stopped())

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
        with self._lock:
            self._stopped = True
            if self._armed:
                faulthandler.cancel_dump_traceback_later()
                self._armed = False

    def _arm(self) -> bool:
        with self._lock:
            if self._stopped:
                return False
            self._deadline = time.monotonic() + self.timeout_seconds
            self._set_timer(self.timeout_seconds)
            return True

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
            while self._arm():
                await asyncio.sleep(self._rearm_seconds)
        finally:
            self.stop()
