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
  hold that the duration backstop owns. App code does not pair these two calls
  by hand — :func:`holding_progress` is the public front door, and it is the
  one piece of ADR-0018 an app author is expected to type.

Both signals are produced deep inside code that has no reference to the
tracker — the SDK's transfer loops, a blocking call offloaded through
``run_in_thread``, an app's own ``holding_progress`` block. They reach the
current attempt's tracker through the ContextVar in this module:
:func:`current_progress_tracker` for consumers, and
:func:`bind_progress_tracker` for ``activities.py``, which owns one tracker per
activity attempt.

The watchdog that consumes the tracker lives in
:func:`~application_sdk.execution.heartbeat.auto_heartbeat_loop` and runs in one
of the three
:class:`~application_sdk.execution.progress.ProgressWatchdogMode` states — that
enum stays in the execution layer, since nothing here reads it and it is the
watchdog's own vocabulary. It is handed the tracker by
injection rather than reading the ContextVar, so it stays unit-testable without
an activity. The framework hooks (FND-288), the ``run_in_thread`` auto-holds
(FND-290) and the warn-mode telemetry that consumes :class:`ClosedHold`
(FND-292) are separate pieces built on this one; :func:`holding_progress`
(FND-291) lives here, next to the tracker whose holds it opens.

In enforce mode the watchdog's verdict comes back through
:meth:`ProgressTracker.flag_stalled`, and that :class:`StallObservation` is what
tells ``activities.py`` that a landing ``asyncio.CancelledError`` is a stall kill
rather than a worker eviction — the two are otherwise indistinguishable at the
handler that sees them.

**Why this module lives in the substrate** (ADR-0019). Its consumers span every
layer — ``storage/`` marks progress from its transfer and writer loops,
``app/`` binds the tracker, ``execution/`` runs the watchdog — so it has to be
importable at module scope from all of them, ``storage/`` included. Under
``execution/`` it was not: importing any ``execution`` submodule runs that
package's ``__init__``, which reaches back into ``storage.ops``. The app-facing
path is still ``application_sdk.execution.progress``, which re-exports every
name below.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: How long a task may go without an observable progress signal, in seconds
#: (ADR-0018 → *Decisions taken*). Roughly app-independent: it answers "how long
#: may this attempt be silent?", not "how much data does this tenant have?".
#:
#: One number with two uses, deliberately not two numbers. The stall watchdog
#: takes it as ``max_no_progress_seconds`` — this is the fallback beneath
#: ``ATLAN_MAX_NO_PROGRESS_SECONDS`` and ``@task(max_no_progress_seconds=...)``,
#: both resolved in :mod:`application_sdk.execution.progress` — and the warn-mode
#: hold report takes it as the floor above which a hold is worth naming in the
#: log: a hold longer than the budget is exactly a hold that *would* have tripped
#: the watchdog had it not been vouched for, which is the work-list criterion.
DEFAULT_MAX_NO_PROGRESS_SECONDS = 900.0


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
class StallObservation:
    """The no-progress gap the watchdog acted on, frozen at the moment it acted.

    Written by the activity layer's ``on_stall`` handler and read by the
    cancellation handler that turns the resulting ``CancelledError`` into a
    typed ``TaskStalledError`` (ADR-0018 → *Failing the activity*). Frozen at
    detection time rather than re-derived at the raise site because by then the
    numbers have moved: :meth:`ProgressTracker.stalled_for` keeps growing while
    the cancellation travels to the attempt's next ``await``, and any progress
    signal in flight would re-arm it entirely.

    Attributes:
        stalled_for_seconds: Seconds without an observable progress signal, as
            observed by the watchdog tick that decided to fail the attempt.
        last_progress_label: The last progress label seen, ``""`` if the attempt
            never reported one.
    """

    stalled_for_seconds: float
    last_progress_label: str


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


# Hold tokens are process-wide unique, not per-tracker. A per-tracker counter
# would let two concurrently bound trackers (a nested attempt, or a test binding
# its own tracker inside an activity) both consider token 0 valid, so a consumer
# that paired one tracker's enter_hold with another's exit_hold would silently
# release the wrong hold. Drawing every token from one shared counter makes a
# token collide with only its owning tracker, so a cross-tracker exit_hold hits
# the unknown-token warning instead. `next()` on the counter is a single
# thread-safe step, so allocation stays correct without taking each tracker's
# lock.
_token_counter = itertools.count()


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
        self._stall: StallObservation | None = None
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
            calls — cannot release each other's deadlines. Tokens are drawn
            from a process-wide counter rather than a per-tracker one, so a
            token is unique across every live tracker: a consumer that pairs
            this ``enter_hold`` with a *different* tracker's :meth:`exit_hold`
            (a rebind in between) hits the unknown-token warning instead of
            silently releasing that tracker's hold.
        """
        if timeout is not None and timeout < 0:
            logger.warning(
                "Hold '%s' was entered with a negative allowance (%.3fs); treating "
                "it as already exhausted — the stall watchdog will not pause for it",
                label,
                timeout,
            )
        now = self._clock()
        token = next(_token_counter)
        with self._lock:
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

    def has_open_hold(self, token: int) -> bool:
        """Whether ``token`` names a hold that has not been released yet.

        Deliberately *not* :meth:`held`: this asks whether one specific hold is
        still open, not whether anything is still vouching. A bounded hold whose
        allowance has lapsed answers ``True`` here and stops counting in
        :meth:`held` — which is what the automatic holds need, since a lapsed
        declared hold must keep them suppressed (re-arming an unbounded auto-hold
        at the moment an allowance lapses would defeat the allowance) while the
        watchdog is free to resume.

        Args:
            token: A token from :meth:`enter_hold`.

        Returns:
            ``True`` while that hold is open on *this* tracker.
        """
        with self._lock:
            return token in self._holds

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

    @property
    def stall(self) -> StallObservation | None:
        """The gap the watchdog failed this attempt for, ``None`` if it has not.

        The one thing that tells the activity's ``except CancelledError`` handler
        *whose* cancellation it is holding: a stall kill and a worker eviction
        arrive at the same handler as the same exception type, and only this
        distinguishes them (ADR-0018 → *Failing the activity*). Read it before
        the shutdown check — a stall and a SIGTERM can coincide, and attributing
        such a cancel to eviction would re-dispatch a wedged attempt outside the
        normal retry budget.
        """
        with self._lock:
            return self._stall

    def flag_stalled(
        self, stalled_for_seconds: float, last_progress_label: str
    ) -> None:
        """Record that the watchdog has judged this attempt stalled.

        Called by the activity layer's ``on_stall`` handler immediately before it
        cancels the attempt, so the observation is already here when the
        cancellation lands. Not progress, and deliberately not a mutation of the
        stall clock: this records a verdict, it does not change what was
        observed.

        First flag wins. A second call cannot happen through the watchdog (the
        loop returns as soon as it enforces), and if it ever did, the
        observation that decided to cancel the attempt is the one worth naming in
        the failure.

        Args:
            stalled_for_seconds: The gap the watchdog observed.
            last_progress_label: The last progress label it saw, ``""`` if none.
        """
        with self._lock:
            if self._stall is None:
                self._stall = StallObservation(
                    stalled_for_seconds=stalled_for_seconds,
                    last_progress_label=last_progress_label,
                )

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

    def has_open_hold(self, token: int) -> bool:
        """Always ``False`` — an inert tracker never opened one.

        Overridden rather than inherited for the same reason the mutators are:
        this instance is a process-wide singleton, and the inherited reader would
        take its shared lock on every offload made outside an activity.
        """
        return False

    def held(self) -> bool:
        """Always ``False`` — an inert tracker vouches for nothing."""
        return False

    def stalled_for(self) -> float:
        """Always ``0.0`` — nothing is watching, so nothing is stalled."""
        return 0.0

    @property
    def stall(self) -> StallObservation | None:
        """Always ``None`` — no watchdog observes this context."""
        return None

    def flag_stalled(
        self, stalled_for_seconds: float, last_progress_label: str
    ) -> None:
        """Record nothing.

        Overridden for the same reason the other mutators are: this instance is a
        process-wide singleton, so a verdict recorded on it would outlive its
        caller and make every later cancellation anywhere in the process read as
        a stall kill.
        """


_INERT_TRACKER = _InertProgressTracker()

_progress_tracker: ContextVar[ProgressTracker | None] = ContextVar(
    "progress_tracker", default=None
)

#: The declared hold covering the current context, as ``(tracker, token)``.
#: Set by :func:`holding_progress` for its block's dynamic extent, which is what
#: makes it answer *"is this work already covered by an allowance a human
#: declared?"* rather than the tracker-wide *"is anything vouching?"* that
#: :meth:`ProgressTracker.held` answers. The distinction matters because the
#: tracker's hold set is flat: it cannot tell an enclosing hold from a concurrent
#: one, and only the enclosing relationship should suppress anything.
#:
#: A *reference to the hold* rather than a boolean, because a boolean can outlive
#: what it describes. ``asyncio.create_task`` copies the creating context
#: (PEP 567), so a task spawned inside the block carries the mark into a lifetime
#: the parent's ``reset`` can never reach — and a *detached* one, still running
#: after the block exits, would then suppress its own auto-hold with no declared
#: hold left to cover it. Naming the hold makes the mark falsifiable: the token
#: is either still open on that tracker or it is not.
_declared_hold: ContextVar[tuple["ProgressTracker", int] | None] = ContextVar(
    "declared_hold", default=None
)


def declared_hold_active() -> bool:
    """Whether an explicit declared allowance is *still* covering this context.

    Read by the automatic holds (FND-290) so they can stand down inside a
    :func:`holding_progress` block. An author who declares an allowance is
    making a statement the SDK must not quietly outvote: an unbounded auto-hold
    added *inside* a bounded one keeps vouching after the declared allowance
    lapses — ``held()`` and ``stalled_for()`` treat the hold set as a union, and
    an unbounded hold never lapses — which would hand a site its author had
    bounded back to the duration backstop.

    Both halves of the check earn their place:

    - **The hold is still open** (:meth:`ProgressTracker.has_open_hold`), not
      merely marked. A task spawned inside the block inherits a *copy* of the
      context, so the mark survives in that task after the block exits and
      releases the hold. Trusting the mark alone would let such a task stand
      down with nothing vouching for it at all — no declared hold, and no
      auto-hold either — which is a worse hole than the one this suppression
      closes. Once the declared hold is released, that task's next offload is
      auto-held again like any other.
    - **It is open on the tracker bound right now.** A hold belonging to a
      different attempt cannot govern this one, so a rebind in between must not
      suppress anything.

    Context-scoped rather than tracker-scoped on purpose. A concurrent task that
    never entered the block does not observe the mark, so its own offload is
    still auto-held; suppressing that one would leave real blocking work
    unvouched, which is the false-kill this whole mechanism exists to prevent.
    """
    declared = _declared_hold.get()
    if declared is None:
        return False
    tracker, token = declared
    return tracker is current_progress_tracker() and tracker.has_open_hold(token)


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


@asynccontextmanager
async def holding_progress(label: str, *, timeout: float | None) -> AsyncIterator[None]:
    """Vouch for one opaque operation for as long as you would let it run.

    The explicit half of ADR-0018, and the only part of it an app author types.
    One context manager covers both shapes of opaque work — an ``await`` against
    the connector's own async client, and a blocking call offloaded through
    ``run_in_thread``:

    .. code-block:: python

        # async: the connector's own client, which the SDK cannot see into
        async with holding_progress("snapshot metadata query", timeout=1800):
            rows = await long_single_query(...)

        # blocking: the same wrapper around the offload
        async with holding_progress("full table scan", timeout=7200):
            rows = await run_in_thread(cursor.execute, sql)

    Inside an ``App`` subclass, reach it as ``self.holding_progress(...)`` or
    ``self.task_context.holding_progress(...)`` — the same two receivers as
    ``run_in_thread``. Import this function directly for app code that sits
    outside the app class.

    Inside the block the stall watchdog is paused for this attempt; on exit the
    completed operation is recorded as progress under ``label``. Past the
    declared allowance the hold **lapses**: the watchdog resumes from the
    deadline and the stall fires ``max_no_progress_seconds`` later, so the
    effective kill time for a wedged call is ``timeout + budget`` rather than the
    duration backstop's 24h.

    **The allowance you declare governs everything inside the block**, including
    the automatic holds ``run_in_thread`` and ``run_fault_isolated`` would
    otherwise add (FND-290): those stand down here, so the blocking example below
    lapses at ``timeout`` like the async one rather than inheriting an unbounded
    auto-hold that would outlive it. That is why the two examples have the same
    kill time despite one of them offloading.

    **Expect to need this in almost every connector.** Blocking calls are
    auto-held because ``run_in_thread`` is a mandatory seam, but async source
    calls have no equivalent SDK-owned seam — a connector talks to its source
    with its *own* async client (async SQLAlchemy, ``httpx.AsyncClient``, a
    vendor SDK), and the SDK's internal SQL/HTTP clients are not on that path.
    Interleaved streaming reads (fetch a page, write a batch, repeat) are
    already covered by the write side and need no hold; the residual this exists
    for is the genuinely opaque *single* call — one large metadata query, one
    slow list/export that returns everything at once. That is a standard part of
    writing a long-running async task, not a rare escape hatch.

    Args:
        label: The call site being vouched for, e.g.
            ``"snapshot metadata query"``. Named in the stall log and in the
            warn-mode hold report, so it must identify a *site* — never
            interpolate a query, a key, a credential or a customer value into
            it.
        timeout: How long you would let this one operation run before you would
            rather it failed — **not** a prediction of how long it takes. That
            question is answerable here because it is a property of one
            operation against one resource, rather than of a tenant's data
            volume. Keyword-only and required: the SDK never derives or defaults
            this number, so declaring nothing is spelled ``timeout=None``, which
            makes the hold unbounded and hands the whole bound to the duration
            backstop — an accepted residual, and one warn mode reports rather
            than hides.

            Err generous. The error is asymmetric: too generous only delays
            detection toward the backstop, while too tight kills a healthy run —
            and because stall kills retry, a too-tight allowance burns the same
            wasted work up to three times. Use warn mode's observed p99 for the
            site plus headroom rather than a number invented at the desk.

    Yields:
        ``None`` — the block's value is the vouch, not an object.

    Note:
        Outside an activity (a local run, a unit test, a script) this is inert:
        there is no attempt to vouch for, so the hold is recorded nowhere and
        nothing observes it.

        The hold is released in a ``finally``, so an exception or a cancellation
        inside the block releases it rather than leaving the watchdog paused for
        the rest of the attempt. Holds are keyed by token rather than stacked, so
        nesting and concurrent blocks — ``asyncio.gather`` over several opaque
        calls — cannot release each other's deadlines.
    """
    # Read the tracker once and release on the same object. Re-reading at exit
    # would pick up whatever the context has bound by then, which for a hold
    # spanning a context change releases the wrong deadline.
    tracker = current_progress_tracker()
    token = tracker.enter_hold(label, timeout)
    declared = _declared_hold.set((tracker, token))
    try:
        yield
    finally:
        # Reset first, then release. Both orders are correct for this context —
        # the mark names this token either way — but releasing last keeps the
        # window in which the mark points at a live hold as small as possible for
        # any task still reading a *copy* of this context.
        _declared_hold.reset(declared)
        tracker.exit_hold(token)
