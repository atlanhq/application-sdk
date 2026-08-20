"""How many tracked activities shared this process, per activity execution.

A cgroup reading is *pod-wide*. Attributing it to one activity is only valid when
one activity was running, and worker concurrency defaults to 100
(``TEMPORAL_MAX_CONCURRENT_ACTIVITIES``) — so most apps never satisfy that.

Rather than force concurrency to 1, which would change throughput on apps that
have nothing to do with tiering, each execution records the **maximum concurrency
it ever saw**. That one number lets the analysis pick the right model instead of
pooling two different ones:

* ``1`` — the peak is this activity's. Fit per-activity.
* ``>1`` — the peak is the pod's. Join rows overlapping on ``pod`` and
  ``started_at`` to recover the in-flight set and its combined input, then fit
  per-pod, which is the only unit a shared pod can be routed to anyway.

Without it both land in one bucket, and an envelope fitted at concurrency 1 keeps
being applied after concurrency rises — the same units meaning something different,
with nothing in the data to say so. AE is on that path already: its
``ATLAN_MAX_CONCURRENT_ACTIVITIES: "1"`` is documented as a temporary Daft
mitigation, to be restored to 3.
"""

from __future__ import annotations

import threading


class _Census:
    """Process-wide count of in-flight tracked activities.

    Each caller gets back the high-water mark for *its own* window, not the count
    at any single instant: an activity that started alone and was joined by three
    others has a pod-wide peak, and a reading taken at entry would call it clean.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, int] = {}
        self._next_token = 0
        self._count = 0

    def enter(self) -> tuple[int, int]:
        """Register an execution. Returns ``(token, concurrency_now)``."""
        with self._lock:
            self._count += 1
            self._next_token += 1
            token = self._next_token
            # Every live execution may now have a higher high-water mark, so they
            # all get updated — not just the one arriving.
            for other in self._active:
                if self._count > self._active[other]:
                    self._active[other] = self._count
            self._active[token] = self._count
            return token, self._count

    def peak(self, token: int) -> int:
        """Max concurrency seen during this window so far, without deregistering.

        Separate from :meth:`leave` because the caller needs the number while still
        holding its slot — reading it by leaving early would drop the count while
        the activity is genuinely still running, undercounting everyone else.
        """
        with self._lock:
            return self._active.get(token, 1)

    def leave(self, token: int) -> int:
        """Deregister and return the max concurrency seen during that window.

        Idempotent: a second call for the same token is a no-op returning ``1``.
        Two callers deregister each execution (the outer wrapper always, the
        measured path for its own bookkeeping), and an unconditional decrement
        would under-count concurrency for every other activity in the process.
        """
        with self._lock:
            if token not in self._active:
                return 1
            seen = self._active.pop(token)
            self._count = max(0, self._count - 1)
            return seen

    def active(self) -> int:
        with self._lock:
            return self._count

    def _reset_for_testing(self) -> None:
        with self._lock:
            self._active.clear()
            self._count = 0
            self._next_token = 0


CENSUS = _Census()
