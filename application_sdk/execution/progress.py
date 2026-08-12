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

Both signals are produced deep inside code that has no reference to the
tracker — the SDK's transfer loops, a blocking call offloaded through
``run_in_thread``, an app's own ``holding_progress`` block. They reach the
current attempt's tracker through the ContextVar in this module:
:func:`current_progress_tracker` for consumers, and
:func:`bind_progress_tracker` for ``activities.py``, which owns one tracker per
activity attempt.

The watchdog that consumes the tracker lives in
:func:`~application_sdk.execution.heartbeat.auto_heartbeat_loop` and runs in one
of the three :class:`ProgressWatchdogMode` states. It is handed the tracker by
injection rather than reading the ContextVar, so it stays unit-testable without
an activity. The framework hooks (FND-288), the ``run_in_thread`` auto-holds
(FND-290), the public ``holding_progress()`` context manager (FND-291) and the
warn-mode telemetry that consumes :class:`ClosedHold` (FND-292) are separate
pieces built on this one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from application_sdk.contracts.base import SerializableEnum
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class ProgressWatchdogMode(SerializableEnum):
    """How the stall watchdog reacts to a no-progress gap (ADR-0018).

    Three states, not two, because the watchdog is also the audit tool that
    tells an app *where* it needs holds — a job that only works if it can
    observe without being able to fail anything.

    ``SerializableEnum`` (a ``StrEnum``) rather than a plain ``Enum``: the mode
    ends up on the task's Temporal payload alongside
    ``heartbeat_timeout_seconds``, and it is used directly as a metric
    attribute value.
    """

    OFF = "off"
    """Inert. Nothing is observed and nothing is reported — byte-identical to
    pre-ADR-0018 behaviour. A kill-switch, not the normal state."""

    WARN = "warn"
    """Report every gap as a metric and an INFO log; never fail an activity.
    The fleet-wide default, and the audit tool that produces each app's
    work-list."""

    ENFORCE = "enforce"
    """Report the gap, then fail the activity through the injected
    ``on_stall`` handler."""


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


class _InertProgressTracker(ProgressTracker):
    """The no-tracker case, as a well-defined no-op.

    Returned by :func:`current_progress_tracker` when no activity attempt owns
    the current context: a local run, a unit test, an HTTP handler, the SDK's
    own transfer loops called from a script. Progress signals from those callers
    are recorded nowhere and no watchdog observes them, so the honest answer to
    *"is anything vouching for this?"* and *"how long has it been quiet?"* is
    ``False`` and ``0.0`` — not the process's uptime.

    Existing as a null object rather than letting the accessor return ``None``
    is deliberate: every consumer of the tracker is a paired
    ``enter_hold`` / ``exit_hold`` or a bare ``mark_progress`` buried in a hot
    loop, and a ``None`` check at each of those sites is the exact thing that
    gets half-written. It mirrors ``NoopHeartbeatController``'s role on the
    heartbeat path.

    The mutators are overridden rather than inherited because this instance is a
    process-wide singleton: inherited ``enter_hold`` would accumulate a hold per
    never-exited token for the life of the process, and inherited
    ``mark_progress`` would take a shared lock on every framework-hook call made
    outside an activity.
    """

    def mark_progress(self, label: str = "") -> None:
        """Discard the signal — nothing is observing this context."""

    def enter_hold(self, label: str, timeout: float | None) -> int:
        """Vouch for nothing, and hand back a token no real tracker can own.

        Real tokens are non-negative, so if a consumer ever pairs this
        ``enter_hold`` with a *real* tracker's ``exit_hold`` — the ContextVar
        having been set in between — the mismatch is logged rather than silently
        releasing somebody else's hold.
        """
        return -1

    def exit_hold(self, token: int) -> None:
        """Release nothing, and stay quiet: there was no hold to release."""

    def held(self) -> bool:
        """Always ``False`` — an inert tracker vouches for nothing."""
        return False

    def stalled_for(self) -> float:
        """Always ``0.0`` — nothing is watching, so nothing is stalled."""
        return 0.0


_INERT_TRACKER = _InertProgressTracker()

_progress_tracker: ContextVar[ProgressTracker | None] = ContextVar(
    "progress_tracker", default=None
)


def current_progress_tracker() -> ProgressTracker:
    """Get the :class:`ProgressTracker` for the current activity attempt.

    The read seam for every progress producer — the framework hooks, the
    ``run_in_thread`` auto-hold, ``holding_progress()`` — none of which is
    handed the tracker by its caller.

    Reachable from anywhere inside the attempt, including from a worker thread:
    ``run_in_thread`` propagates the calling context via
    ``contextvars.copy_context()``, so blocking work offloaded through it reads
    the same tracker as the coroutine that offloaded it. Mutations inside that
    thread stay in the thread's copy of the context (copy semantics), but the
    tracker *object* is shared, so progress marked from the thread lands on the
    attempt's tracker.

    Returns:
        The current attempt's tracker, or an inert one when no attempt owns this
        context (local runs, unit tests, non-activity callers). Never ``None``
        and never raises, so a caller outside an activity behaves exactly as it
        did before this plumbing existed.

    Note:
        Callers that pair ``enter_hold`` with ``exit_hold`` must hold on to the
        object this returns and call both on it, rather than calling this twice.
        Re-reading picks up whatever tracker the context has by then, which for
        a hold spanning a context change would release the wrong deadline.
    """
    tracker = _progress_tracker.get()
    return _INERT_TRACKER if tracker is None else tracker


@contextmanager
def bind_progress_tracker(tracker: ProgressTracker) -> Iterator[ProgressTracker]:
    """Bind ``tracker`` as the current attempt's tracker for the block's extent.

    Entered once per activity attempt by
    ``application_sdk.execution._temporal.activities``, which owns the tracker's
    lifetime. Each activity runs as its own asyncio task with its own copy of
    the context, so concurrent activities in one worker bind their own tracker
    and never observe each other's.

    A block rather than a ``set``/``reset`` pair, because a bind that outlives
    its attempt is a silent bug of exactly the wrong shape: the next caller in
    that context reports progress into a finished attempt's tracker, and pairing
    an ``enter_hold`` across the boundary releases the wrong deadline. Nothing
    here can leave a binding behind, and there is no token for a caller to
    mislay — the pairing is not something a call site can get wrong.

    Restores whatever was bound before, so a nested bind — or a test binding its
    own tracker — leaves the caller's binding intact.

    Args:
        tracker: The tracker for this attempt.

    Yields:
        The same ``tracker``, so the owner can hold on to it for the pieces that
        take it by injection rather than through the ContextVar (the stall
        watchdog in ``auto_heartbeat_loop``).
    """
    token = _progress_tracker.set(tracker)
    try:
        yield tracker
    finally:
        _progress_tracker.reset(token)
