"""Process-wide record of whether this worker's poll loop is alive.

Written by the poll-state observer in :mod:`application_sdk.main`; read by
:class:`~application_sdk.execution._temporal.auth.TemporalAuthManager` before it
publishes a ``token_refresh`` lifecycle event.

Why this exists
---------------

A worker whose poll loop has died keeps refreshing its Temporal token and keeps
answering ``/live`` and ``/ready``. The ``token_refresh`` lifecycle events it
emits reach the fleet agent registry, which stamps them as the agent's last
health update — so a worker doing no work at all goes on advertising itself as
healthy, indefinitely, and the fleet inventory has no way to tell it apart from
a genuinely idle one. Withholding those events while the poll loop is
confirmed dead is what turns that silent state into a visible one.

Gating posture
--------------

Deliberately conservative, because a false positive marks a *healthy* agent
dead across the fleet:

* Only a **definitive** zero-poller reading counts. ``unknown`` never does —
  sdk-core registers the poller gauge only after the first successful poll, so
  ``unknown`` is both the normal startup state and what a transient gauge-read
  failure looks like.
* Suppression engages only after several **consecutive** zero readings, so a
  brief blip or a slow start cannot trip it.
* Anything that is not a definitive zero — a resumed poll, an unknown reading,
  a shutdown — clears the state immediately. The default posture is to publish.

This is a leaf module: it imports nothing from the SDK beyond the logger, so
both :mod:`application_sdk.main` and the auth manager can import it without a
cycle.
"""

from __future__ import annotations

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Observer reading meaning "the poller gauge says zero pollers are live".
POLL_STATE_ZERO = "zero"


class WorkerPollState:
    """Tracks consecutive zero-poller readings and gates health events on them.

    Not thread-safe by design: the observer is the only writer and both it and
    the auth refresh loop run on the same event loop.
    """

    #: Consecutive definitive-zero readings before health events are withheld.
    DEFAULT_ZERO_READINGS_BEFORE_STALE = 3

    def __init__(self) -> None:
        self._threshold = self.DEFAULT_ZERO_READINGS_BEFORE_STALE
        self._consecutive_zero = 0
        self._suppressed = False

    def configure(self, *, zero_readings_before_stale: int) -> None:
        """Set how many consecutive zero readings withhold health events."""
        self._threshold = max(1, int(zero_readings_before_stale))

    def record(self, state: str) -> None:
        """Record one observer reading (``polling`` / ``zero`` / ``unknown``)."""
        if state == POLL_STATE_ZERO:
            self._consecutive_zero += 1
            if not self._suppressed and self._consecutive_zero >= self._threshold:
                self._suppressed = True
                logger.warning(
                    "Worker has reported 0 active pollers %d times in a row; "
                    "withholding token_refresh health events so this worker "
                    "stops advertising itself as a healthy agent while it is "
                    "doing no work. Token refresh itself continues. Events "
                    "resume automatically as soon as polling does (ARUN-1127)",
                    self._consecutive_zero,
                )
            return

        # Not a definitive zero: fail open. Covers a resumed poll loop and an
        # unreadable gauge alike — neither is evidence that this worker is dead.
        if self._suppressed:
            logger.info(
                "Worker poll state recovered to %r; resuming token_refresh "
                "health events",
                state,
            )
        self._consecutive_zero = 0
        self._suppressed = False

    def reset(self) -> None:
        """Clear all state and resume publishing (used on shutdown and in tests)."""
        self._consecutive_zero = 0
        self._suppressed = False

    def should_emit_health_event(self) -> bool:
        """False only while the poll loop is confirmed dead."""
        return not self._suppressed


#: Process-wide instance. The observer writes it; the auth manager reads it.
worker_poll_state = WorkerPollState()
