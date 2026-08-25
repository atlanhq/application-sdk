"""Max concurrency each activity execution saw, so a row says whether its
pod-wide peak is attributable to it alone (``1``) or to the pod (``>1``).
"""

from __future__ import annotations

import threading


class _Census:
    """High-water mark per window, not an instant count: an activity joined
    later still has a pod-wide peak, which an entry-time reading would miss.
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
            # Every live execution's high-water mark may rise, not just this one.
            for other in self._active:
                if self._count > self._active[other]:
                    self._active[other] = self._count
            self._active[token] = self._count
            return token, self._count

    def peak(self, token: int) -> int:
        """Max concurrency seen so far, without deregistering — leaving early to
        read it would undercount everyone else while this activity still runs.
        """
        with self._lock:
            return self._active.get(token, 1)

    def leave(self, token: int) -> int:
        """Deregister and return the window's max concurrency. Idempotent, because
        two callers deregister each execution and a double decrement undercounts.
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
