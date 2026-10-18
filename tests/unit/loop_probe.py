"""Deterministic proof that the event loop stayed free during a blocking call.

ADR-0010's property is "the loop can run other coroutines — the auto-heartbeat —
while blocking work is in progress". The earlier tests measured it by counting
ticks of an ``asyncio.sleep`` ticker over a fixed ``time.sleep``, which measures
how much CPU and GIL the loop thread got on the runner, not where the work ran.
A correctly offloaded block still scored too few ticks on a loaded shared macOS
runner and failed one matrix cell at random (FND-360, FND-4018).

:class:`LoopProbe` asserts the property directly, with no wall-clock threshold
on the passing path. The patched blocking primitive calls :meth:`LoopProbe.block`
in place of ``time.sleep``. That hands the loop a callback and waits for the
loop to run it:

* Offloaded to a worker thread: the loop is free, runs the callback, and the
  wait returns at once.
* Still on the loop thread: the loop is the thread that is waiting, so the
  callback cannot run until the wait gives up. The probe records that.

The timeout only decides how long a genuine regression takes to fail. It plays
no part in whether a healthy run passes.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

#: How long a block waits for the loop before recording a stall. GIL contention
#: alone delays a free loop by about a second at most, so this is far above any
#: healthy delay. A regression costs this long once per blocked call.
STALL_SECONDS = 10.0


class LoopProbe:
    """Records, per blocking call, whether the event loop ran while it waited."""

    def __init__(self, stall_seconds: float = STALL_SECONDS) -> None:
        self.stall_seconds = stall_seconds
        self.outcomes: list[bool] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self, work: Callable[[], Awaitable[T]]) -> T:
        """Await *work* with this probe bound to the running loop."""
        self._loop = asyncio.get_running_loop()
        try:
            return await work()
        finally:
            self._loop = None

    def block(self) -> None:
        """Stand-in for the blocking cost; call it from the patched primitive."""
        loop = self._loop
        if loop is None:
            raise RuntimeError("LoopProbe.block() called outside LoopProbe.run()")
        released = threading.Event()
        loop.call_soon_threadsafe(released.set)
        self.outcomes.append(released.wait(self.stall_seconds))

    def assert_loop_stayed_free(self, what: str) -> None:
        assert self.outcomes, f"{what}: the blocking path never ran"
        stalled = self.outcomes.count(False)
        assert not stalled, (
            f"{what} held the event loop: {stalled} of {len(self.outcomes)} "
            f"blocking call(s) waited {self.stall_seconds}s for the loop to run "
            "a callback. A held loop cannot run the auto-heartbeat, and that is "
            "what gets healthy activities killed on heartbeat_timeout."
        )
