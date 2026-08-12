"""Observed forward progress for one activity attempt (ADR-0018).

This is the foundation object for the stall watchdog. It answers one question —
*how long has it been since this attempt did anything observable?* — and it
answers it with **no Temporal dependency**, so local runs, unit tests and
production exercise the same code path, and a task with
``heartbeat_timeout_seconds=None`` still gets a stall watchdog.

Progress tracking is deliberately a new object rather than a change to
:class:`~application_sdk.execution.heartbeat.HeartbeatController`: the
controller's job (send beats to Temporal) is unchanged, so its Protocol and
``NoopHeartbeatController`` keep their current contract.

Two signals feed it (see ADR-0018 → *Feeding the tracker*):

- :meth:`ProgressTracker.mark_progress` — one observable unit of work
  completed: a batch written, a chunk or file transferred, a page fetched, an
  explicit ``context.heartbeat()``. Never wall-clock time, and never per
  record: batch, chunk and page boundaries only.
- :meth:`ProgressTracker.enter_hold` / :meth:`ProgressTracker.exit_hold` — a
  *vouch* for one in-flight operation the SDK cannot see into (a single large
  query, one slow API call, a blocking call offloaded through
  ``run_in_thread``). The SDK never invents the allowance; a human declares it
  at the one call site that knows, or declares nothing and gets an unbounded
  hold that the duration backstop owns.

The watchdog itself (FND-286), the ContextVar plumbing that makes the tracker
reachable from app code (FND-287), the framework hooks (FND-288) and the
warn-mode telemetry that consumes :class:`ClosedHold` (FND-292) are separate
pieces built on this one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ClosedHold:
    """One completed hold, observed at the moment its token was released.

    Handed to the ``on_hold_closed`` observer so warn mode can rank hold sites
    without anyone reading code. The shape that needs action is a *long
    unbounded* hold: it is invisible to any other audit precisely because it is
    auto-vouched-for, and it is the site that wants an explicit allowance
    instead of relying on the duration backstop.

    Attributes:
        label: The label the hold was entered with (a call site, not a value).
        duration_seconds: Wall-clock seconds the operation actually took.
        allowance_seconds: The allowance a human declared, or ``None`` for an
            unbounded hold.
    """

    label: str
    duration_seconds: float
    allowance_seconds: float | None

    @property
    def bounded(self) -> bool:
        """Whether an allowance was declared for this hold."""
        return self.allowance_seconds is not None

    @property
    def lapsed(self) -> bool:
        """Whether the operation outlived its declared allowance.

        A lapsed hold means the watchdog resumed while the operation was still
        running — the allowance was too tight, or the source really did hang.
        Always ``False`` for an unbounded hold.
        """
        return (
            self.allowance_seconds is not None
            and self.duration_seconds > self.allowance_seconds
        )


@dataclass(frozen=True)
class _Hold:
    """An in-flight vouch. ``allowance_seconds=None`` means unbounded."""

    label: str
    started_at: float
    allowance_seconds: float | None

    @property
    def deadline(self) -> float | None:
        """Clock reading past which this hold no longer vouches for anything."""
        if self.allowance_seconds is None:
            return None
        return self.started_at + self.allowance_seconds


class ProgressTracker:
    """Observed forward progress for one activity attempt.

    One instance per activity attempt. Cheap: a dict, two floats and an
    uncontended lock.

    Args:
        clock: Monotonic time source, in seconds. Injected rather than read from
            a patchable global on purpose: an asyncio loop shares
            ``time.monotonic``, so patching it globally in tests makes the loop
            itself misbehave (flaky ``StopIteration`` from an exhausted
            side-effect list). Tests advance their own clock instead.
        on_hold_closed: Called once per :meth:`exit_hold` with the observed
            :class:`ClosedHold`. This is the warn-mode audit seam (FND-292);
            it defaults to no observer. Exceptions raised by the observer are
            logged and swallowed — telemetry must never fail an activity.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        on_hold_closed: Callable[[ClosedHold], None] | None = None,
    ) -> None:
        self._clock = clock
        self._on_hold_closed = on_hold_closed
        self._last_label: str = ""
        self._last_at: float = clock()
        self._holds: dict[int, _Hold] = {}
        self._next_token: int = 0
        # Mutations are not confined to the event loop: contextvars propagate
        # into `run_in_thread`, so framework hooks and `context.heartbeat()`
        # inside an offloaded blocking call reach this object from a worker
        # thread. Token allocation especially must be serialised — two holds
        # sharing a token would let one release the other's deadline.
        self._lock = threading.Lock()

    @property
    def last_label(self) -> str:
        """Label of the most recent labelled progress signal, ``""`` if none.

        Named in the stall log and the stall metric so an operator sees *where*
        the attempt went quiet rather than only *that* it did.
        """
        with self._lock:
            return self._last_label

    def mark_progress(self, label: str = "") -> None:
        """Record one observable unit of work completing.

        Args:
            label: What made progress, e.g. ``"write_batch"``. An empty label
                records the progress but leaves :attr:`last_label` alone, so
                warn mode can re-arm the stall clock after reporting a gap
                without erasing the signal it just reported.
        """
        with self._lock:
            now = self._clock()
            self._last_at = max(self._last_at, now)
            if label:
                self._last_label = label

    def enter_hold(self, label: str, timeout: float | None) -> int:
        """Vouch for an in-flight operation the SDK cannot see into.

        Args:
            label: The call site being vouched for, e.g.
                ``"snapshot metadata query"``. Carried into the stall log and
                the :class:`ClosedHold` observation, so make it identify a
                site — never interpolate a query, a credential or a customer
                value into it.
            timeout: How long the caller would let this one operation run
                before it would rather it failed — *not* a prediction of how
                long it takes. ``None`` is an unbounded hold: the stall
                watchdog is inactive for the whole call and the duration
                backstop is its only bound (an accepted residual, surfaced by
                warn mode). A negative allowance is a programming error; it is
                logged and treated as already exhausted.

        Returns:
            An opaque token to pass to :meth:`exit_hold`. Holds are keyed by
            token, never popped off a stack, so concurrent holds in one
            activity — ``asyncio.gather`` over several ``run_in_thread``
            calls — cannot release each other's deadlines.
        """
        if timeout is not None and timeout < 0:
            logger.warning(
                "Hold '%s' was entered with a negative allowance (%.3fs); treating "
                "it as already exhausted — the stall watchdog will not pause for it",
                label,
                timeout,
            )
        now = self._clock()
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._holds[token] = _Hold(
                label=label, started_at=now, allowance_seconds=timeout
            )
        return token

    def exit_hold(self, token: int) -> None:
        """Release the hold ``token`` vouched for.

        A completed operation *is* progress, so this marks progress under the
        hold's label. It also reports the observed duration to
        ``on_hold_closed``, which is what makes long unbounded holds visible to
        the warn-mode report — without it, blocking work auto-held by
        ``run_in_thread`` would be invisible to the audit precisely because it
        was vouched for.

        Args:
            token: A token returned by :meth:`enter_hold`. An unknown or
                already-released token means the hold plumbing is broken (stall
                accounting is then wrong in the *lenient* direction); it is
                logged and otherwise ignored.
        """
        with self._lock:
            now = self._clock()
            hold = self._holds.pop(token, None)
            if hold is not None:
                self._last_at = max(self._last_at, now)
                if hold.label:
                    self._last_label = hold.label
        if hold is None:
            # The token value is deliberately not logged: "token" reads as a
            # credential to the L010 security scanner, and a per-attempt counter
            # adds nothing an operator can act on.
            logger.warning(
                "exit_hold() found no such hold — it was already released, or the "
                "token came from a different tracker; stall accounting may be lenient"
            )
            return
        self._notify_hold_closed(
            ClosedHold(
                label=hold.label,
                duration_seconds=max(0.0, now - hold.started_at),
                allowance_seconds=hold.allowance_seconds,
            )
        )

    def held(self) -> bool:
        """Whether any hold is currently vouching for the attempt.

        An unbounded hold always vouches. A bounded one stops vouching once its
        allowance is spent, even though its token is still open — that is how a
        wedged call inside a hold is eventually caught instead of being excused
        forever.
        """
        now = self._clock()
        with self._lock:
            return any(
                hold.deadline is None or hold.deadline > now
                for hold in self._holds.values()
            )

    def stalled_for(self) -> float:
        """Seconds since the last observable progress, ``0.0`` while vouched-for.

        A lapsed bounded hold *resumes* the stall clock from its deadline rather
        than from the last progress signal before it: the allowance vouched for
        everything up to the deadline, so the earliest instant the attempt can
        be accused of stalling is the deadline itself. That is what makes the
        effective kill time for a wedged held call ``allowance + budget``
        (ADR-0018 → *Holds*) instead of firing the moment the allowance lapses.
        """
        now = self._clock()
        with self._lock:
            since = self._last_at
            for hold in self._holds.values():
                deadline = hold.deadline
                if deadline is None or deadline > now:
                    return 0.0
                # A hold only vouches forward from where it started, so a
                # non-positive allowance vouches for nothing and must not
                # forgive the quiet that preceded it.
                if deadline > hold.started_at:
                    since = max(since, deadline)
        return max(0.0, now - since)

    def _notify_hold_closed(self, closed: ClosedHold) -> None:
        observer = self._on_hold_closed
        if observer is None:
            return
        try:
            observer(closed)
        # conformance: ignore[E004] best-effort audit telemetry; an observer that
        # raises must never fail the activity it is only observing. Logged at
        # WARNING with the traceback because a broken observer means the
        # warn-mode work-list is silently incomplete.
        except Exception:
            logger.warning(
                "Hold-closed observer failed for hold '%s' (%.3fs); the warn-mode "
                "report will be missing this observation",
                closed.label,
                closed.duration_seconds,
                exc_info=True,
            )
