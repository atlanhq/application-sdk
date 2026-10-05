"""The SDK's offload seam — four sanctioned primitives (ADR-0010, ADR-0017).

``run_in_thread`` offloads blocking calls so they don't starve the event loop
and the heartbeat that rides on it; ``submit_in_thread`` is its detached variant
for cleanup that cancellation must not be able to skip; ``run_fault_isolated``
runs work in a child process so a native fault can't kill the worker; and
``run_best_effort`` is the policy layer over it for non-essential work — it
isolates *and* swallows failures so best-effort work can never break the caller.

**Why this module lives in the substrate** (ADR-0019). Every layer offloads:
``storage/`` (writers, transfer loops, integrity hashing), ``clients/`` (SQL
drivers, Azure SDK), ``handler/``, ``observability/``, the SQL templates.
``run_in_thread`` is a *mandatory* seam per ADR-0010, so a layer that cannot
import it at module scope is a layer that cannot follow the rule cleanly. Under
``execution/`` ``storage/`` could not: importing any ``execution`` submodule runs
that package's ``__init__``, which reaches back into ``storage.ops``. The
app-facing path is still ``application_sdk.execution.heartbeat``, which
re-exports the four primitives; SDK-internal callers import this module.
"""

import asyncio
import concurrent.futures
import contextlib
import contextvars
import functools
import multiprocessing
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Generic, ParamSpec, TypeVar

from application_sdk._runtime.progress import (
    DEFAULT_MAX_NO_PROGRESS_SECONDS,
    ProgressTracker,
    current_progress_tracker,
    declared_hold_active,
)
from application_sdk.errors import InvalidInputValueError
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter, get_logger

logger = get_logger(__name__)

T = TypeVar("T")
P = ParamSpec("P")

# Dedicated executor for blocking operations dispatched via run_in_thread().
#
# Why not None (asyncio's default executor)?
#   Temporal's Python SDK uses the event loop's default executor for its own
#   internal scheduling.  Sharing that pool with long-running blocking calls
#   (database queries, metadata extractions) can exhaust it and deadlock the
#   worker, especially when multiple activities are running concurrently.
#
# Why not a per-call ThreadPoolExecutor?
#   Creating one per call and calling shutdown(wait=False) leaks threads:
#   the executor object is detached but live threads are not joined and
#   accumulate over the lifetime of a worker process.
#
# This single instance is created once at module import and intentionally
# outlives individual calls.  Named threads ("sdk-blocking-N") make it
# distinguishable from Temporal's "activity-pool-N" threads in stack traces.
_BLOCKING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=min(32, (os.cpu_count() or 1) + 4),
    thread_name_prefix="sdk-blocking-",
)

# Executor for CancelHandle actions — a driver cancel (cursor.cancel(), a
# statement-cancel request) fired when the task awaiting a run_in_thread call is
# cancelled.
#
# Why not _BLOCKING_EXECUTOR?
#   The cancel matters most when that pool is saturated by the very calls being
#   abandoned. Queued behind them, the cancel would run only once one of them
#   finished on its own — which is what it exists to avoid.
#
# Why not the loop?
#   A driver cancel is a blocking network round trip.
#
# Small, because an action is one short call; created at import like the pool
# above, and for the same reason.
_CANCEL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="sdk-cancel-",
)


class CancelHandle:
    """Cancel a blocking call at the driver when its awaiting task is cancelled.

    ``run_in_thread`` cannot stop its worker thread: Python has no way to kill
    one. Cancelling the awaiting task hands the loop back at once, but the
    thread runs on, holding a pool slot, a connection and server-side compute
    until the call returns by itself. A handle closes that gap for calls whose
    driver *can* be told to stop::

        handle = CancelHandle()

        def _query() -> list[Row]:
            cursor = connection.cursor()
            handle.set(cursor.cancel)  # from the worker thread, once it exists
            cursor.execute(sql)
            return cursor.fetchall()

        rows = await run_in_thread(handle.bind(_query))

    :meth:`bind` attaches the handle to the callable rather than to
    ``run_in_thread``'s own arguments, so every keyword still reaches ``func``.
    When the awaiting task is cancelled, ``run_in_thread`` calls
    :meth:`request`, which runs the registered action on a small dedicated
    ``sdk-cancel-`` pool — never on the event loop, and never on the
    ``sdk-blocking-`` pool, which is likely full of exactly the calls being
    abandoned. The driver call then fails or returns early in its own thread,
    and the thread and its pool slot are freed.

    **The action is set from the worker thread**, because that is usually the
    only place the driver handle exists, and often only part-way through the
    call (a query id known only after submit). If a cancel was requested before
    :meth:`set` is called, the action is fired as soon as it is set.

    Each action fires at most once. A failing action is logged at WARNING and
    never raised: by then the caller has already been cancelled.

    Thread-safe. One handle per call; it is not reusable across calls.

    **Never stop or suspend a warehouse from an action.** A cancel stops *this*
    statement. A warehouse that the statement started resuming keeps resuming,
    and stopping it would take it away from every other user of it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._action: Callable[[], None] | None = None
        self._requested = False

    @property
    def requested(self) -> bool:
        """Whether a cancel has been requested.

        Worker code checks this between steps (a ``fetchmany`` loop, a batch of
        statements) to stop work nobody is waiting for.
        """
        return self._requested

    def bind(self, func: Callable[P, T]) -> Callable[P, T]:
        """*func*, carrying this handle to :func:`run_in_thread`.

        The returned callable calls *func* with whatever it is given, so
        ``run_in_thread(handle.bind(func), *args, **kwargs)`` passes every
        argument through unchanged; ``run_in_thread`` only reads the handle off
        it. Called directly, it behaves exactly like *func*.
        """
        return _BoundCall(func, self)

    def set(self, action: Callable[[], None]) -> None:
        """Register the driver-level cancel for the call in progress.

        Call this from the worker thread as soon as the driver handle exists.
        A later ``set`` replaces an action that has not fired. If a cancel was
        already requested, ``action`` is fired immediately instead of stored.

        Args:
            action: Zero-argument callable that tells the driver to stop, e.g.
                ``cursor.cancel``. Must not stop or suspend a warehouse.
        """
        with self._lock:
            if not self._requested:
                self._action = action
                return
        _fire_cancel_action(action)

    def request(self) -> None:
        """Request a cancel: fire the registered action, if any, at most once.

        Called by ``run_in_thread`` when its awaiting task is cancelled. Returns
        without waiting for the action, which runs on the ``sdk-cancel-`` pool.
        """
        with self._lock:
            self._requested = True
            action, self._action = self._action, None
        if action is not None:
            _fire_cancel_action(action)


class _BoundCall(Generic[P, T]):
    """A callable carrying the :class:`CancelHandle` it was bound to."""

    __slots__ = ("func", "handle")

    def __init__(self, func: Callable[P, T], handle: CancelHandle) -> None:
        self.func = func
        self.handle = handle

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        return self.func(*args, **kwargs)


def _bound_handle(func: Callable[..., Any]) -> CancelHandle | None:
    """The handle *func* was bound to, looking through ``functools.partial``."""
    target: Any = func
    while isinstance(target, functools.partial):
        target = target.func
    return target.handle if isinstance(target, _BoundCall) else None


def _fire_cancel_action(action: Callable[[], None]) -> None:
    """Run a cancel action on the cancel pool; log, never raise, a failure."""
    try:
        _CANCEL_EXECUTOR.submit(_run_cancel_action, action)
    except RuntimeError:
        # Shut down at interpreter exit; the process is going, and its
        # connections with it.
        logger.debug("Cancel pool is shut down; skipped cancel action", exc_info=True)


def _run_cancel_action(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:
        logger.warning(
            "Cancel action %s failed; the blocking call it was meant to stop "
            "may run to completion",
            _offloaded_callable_name(action),
            exc_info=True,
        )


#: Label prefixes for the auto-holds on the two offload seams (ADR-0018 →
#: *Feeding the tracker*, mechanism 2). The offloaded callable's own name is
#: appended, so warn mode ranks *sites* — "run_in_thread.Cursor.execute, p99
#: 40min, unbounded" is a work-list entry — instead of collapsing every blocking
#: call in an app into one bucket. Naming the seam is part of the signal: it
#: tells an operator which primitive to wrap in ``holding_progress()``.
_THREAD_HOLD_PREFIX = "run_in_thread."
_ISOLATED_HOLD_PREFIX = "run_fault_isolated."

#: What a dotted Python qualname can contain, and nothing else: identifier
#: characters, the dots joining them, and the angle brackets CPython puts around
#: ``<lambda>`` and ``<locals>``. A name carrying anything outside this — a
#: space, a quote, a path separator, a newline — is not a code identifier, so it
#: is not something this label may repeat.
_CALLABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.<>]+$")

#: Longest callable name kept in a label. Real qualnames are short; the deepest
#: shape in the SDK's own tests (a lambda nested in a test method) is under 90.
_MAX_CALLABLE_NAME_LENGTH = 120

#: Stand-in for a callable whose name is unusable. A stable constant, so a name
#: this function refuses adds exactly one series to a metric rather than one per
#: distinct value.
_UNNAMEABLE_CALLABLE = "<callable>"


def _offloaded_callable_name(func: Callable[..., Any]) -> str:
    """Name the offloaded callable, for its auto-hold label.

    Reads the callable's own ``__qualname__`` rather than inspecting the caller's
    frame. All three ``run_in_thread`` entry points funnel into one
    implementation, so a frame walk would name an SDK wrapper on two of the
    three; and the callee is the half of the site an operator can act on anyway.

    Only ever a code identifier — never an argument — so no query, path,
    credential or customer value can reach a hold label.

    Each fallback exists because something real lacks a usable ``__qualname__``:
    ``functools.partial`` carries no name of its own (the wrapped callable
    does), a callable *instance* has one only on its class, and a ``Mock``
    fabricates a child mock for whatever attribute is asked of it, which would
    otherwise put a repr into a label.

    The name is then checked against what a qualname can actually look like, and
    replaced with :data:`_UNNAMEABLE_CALLABLE` if it is not. ``__qualname__`` is
    an ordinary writable attribute, so "it comes from code" is a convention
    rather than a guarantee — a decorator, a factory or a dynamically-built
    class can set it to anything, including a per-call value. That matters here
    and not at a normal call site: this string lands in the stall log and in the
    ``last_label`` *metric attribute*, where a per-call value is unbounded
    series cardinality and a long one is dead weight on every stall report. This
    is defence in depth, not a live threat — but the guarantee this function
    advertises is cheap to actually enforce.
    """
    target: Any = func
    while isinstance(target, (functools.partial, _BoundCall)):
        target = target.func
    for attribute in ("__qualname__", "__name__"):
        name = getattr(target, attribute, None)
        if isinstance(name, str) and _is_usable_callable_name(name):
            return name
    fallback = type(target).__name__
    return fallback if _is_usable_callable_name(fallback) else _UNNAMEABLE_CALLABLE


def _is_usable_callable_name(name: str) -> bool:
    """Whether ``name`` is short enough and shaped like a dotted qualname."""
    return (
        0 < len(name) <= _MAX_CALLABLE_NAME_LENGTH
        and _CALLABLE_NAME_PATTERN.match(name) is not None
    )


@contextlib.contextmanager
def _auto_hold(label: str, timeout: float | None) -> Iterator[None]:
    """Vouch for an offload — unless a human already vouched for it.

    The automatic half of ADR-0018's mechanism 2, shared by both offload seams so
    the precedence rule below cannot be right at one and wrong at the other.

    **An explicit allowance wins.** Inside a ``holding_progress`` block the auto-
    hold stands down entirely and the declared hold governs. It has to: the
    tracker's hold set is a union — ``held()`` and ``stalled_for()`` say
    "vouched" while *any* hold is unlapsed — so an unbounded auto-hold nested
    inside a bounded declared one keeps vouching after the declared allowance
    lapses. The author asked for a wedged call to be caught at
    ``timeout + budget``; adding a hold they did not ask for would silently hand
    the site back to the duration backstop, which is the exact outcome
    ``holding_progress`` exists to prevent. Standing down is also the honest
    reading: the operation *is* already vouched for, by someone who knows the
    number.

    That leaves exactly one hold around the offload rather than two, so the
    warn-mode work-list ranks the site an author can act on — the declared one —
    instead of double-counting the same wall-clock under a second label.

    The suppression is context-scoped (:func:`declared_hold_active`), never
    tracker-scoped: a concurrent offload in a task that never entered the block
    still gets its own hold. Suppressing on "some hold is open somewhere on this
    tracker" would leave real blocking work unvouched and re-introduce the
    false-kill this mechanism exists to remove.

    Args:
        label: Hold label — a site, never a value.
        timeout: Allowance for the hold, or ``None`` for an unbounded one.

    Yields:
        Nothing; the caller runs its offload inside the block.
    """
    if declared_hold_active():
        yield
        return
    # The tracker is read once and both calls go to that same object. Re-reading
    # for the release would pick up whatever tracker the context has by then,
    # which for a hold spanning a context change releases the wrong deadline.
    tracker = current_progress_tracker()
    hold = tracker.enter_hold(label, timeout)
    try:
        yield
    finally:
        # `finally`, not the success path: an offload that raises, and an
        # activity cancelled mid-offload, must still release *their own* token.
        # A leaked unbounded hold vouches for the rest of the attempt, which
        # turns one exception into a watchdog that never fires again.
        tracker.exit_hold(hold)


async def run_in_thread(
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Last-resort escape hatch: run a blocking function in a thread pool.

    .. warning::
        **Use only when no async-native alternative exists.** This is the
        bottom of the preference list, not the default tool for "I have I/O
        to do". Per ADR-0010 (async-first design), the SDK runs on Temporal's
        asyncio event loop; blocking the loop breaks auto-heartbeats and
        causes activities to be retried even though they are making progress.

    **Decision order for blocking work (apps and SDK alike):**

    1. **Prefer an async-native library.** If one exists, use it. No
       ``run_in_thread`` needed:

       =========================  ======================  ====================
       Need                       Use (async)             Avoid (blocking)
       =========================  ======================  ====================
       HTTP requests              ``httpx``, ``aiohttp``  ``requests``
       AWS SDK                    ``aioboto3``,           ``boto3``
                                  ``aiobotocore``
       PostgreSQL                 ``asyncpg``             ``psycopg2``
       MySQL                      ``aiomysql``            ``pymysql``
       File I/O                   ``aiofiles``            ``open()``
       =========================  ======================  ====================

    2. **Then check the SDK.** Many helpers are already async — for example,
       ``self.context.storage`` (ObjectStore), ``self.context.state``
       (StateStore), and credential resolution all expose ``await``-able
       methods. Don't wrap them in ``run_in_thread``.
    3. **Only then** fall back to ``run_in_thread`` — and only after
       confirming there is no async-native alternative for the library
       you're calling.

    **Examples of incorrect use (do not do this):**

    .. code-block:: python

        # WRONG — boto3 has aioboto3; use that instead.
        await self.task_context.run_in_thread(s3_client.put_object, ...)

        # WRONG — requests has httpx; use that instead.
        await self.task_context.run_in_thread(requests.get, url, timeout=30)

    **Behavior:**

    - ContextVars (ObjectStore, logger context, correlation ID, infrastructure
      handles) are propagated to the worker thread via
      ``contextvars.copy_context()``. Mutations inside the thread stay
      isolated from the caller (copy semantics).
    - Threads run on a dedicated ``sdk-blocking-*`` pool, separate from
      Temporal's activity pool, to avoid deadlocking the worker.
    - The offload is automatically wrapped in an **unbounded** progress hold
      (ADR-0018), so the stall watchdog never accuses a legitimately long
      blocking call of stalling. Nothing to do at the call site, and nothing
      about a long blocking call behaves differently than it did before the
      watchdog existed. The SDK does not invent a duration for somebody else's
      blocking call, so the watchdog is inactive for the call's whole duration
      and the activity's duration bound is the only thing still holding it.
      If you can say how long you would let this one call run before you would
      rather it failed, declare it — wrap the offload in
      ``holding_progress(label, timeout=...)``
      (:func:`application_sdk.execution.progress.holding_progress`) and a wedged
      call is caught at ``timeout`` + the no-progress budget instead of at the
      backstop. Inside such a block the automatic hold **stands down** and your
      declared allowance governs; the SDK never adds a vouch that would outlive
      the one you asked for.

    **CRITICAL: your blocking code MUST have its own timeout.**
    Python threads cannot be forcibly killed. If the wrapped call hangs
    forever, the thread runs forever — this orphans state and consumes
    pool slots even after the activity is retried.

    **Cancelling at the driver.** Cancelling the awaiting task returns control
    at once, but the thread runs on. If the driver can stop the call from
    another thread, bind a :class:`CancelHandle` to ``func``
    (``run_in_thread(handle.bind(func), ...)``) and register the driver's
    cancel on it from inside ``func``; the handle is fired when the awaiting
    task is cancelled, and ``CancelledError`` is re-raised as usual. The handle
    travels on the callable, never in ``**kwargs``, so every keyword argument
    still reaches ``func`` — no name is reserved.

    Args:
        func: Blocking function to run. MUST have internal timeout handling.
            A :meth:`CancelHandle.bind` result is cancelled at the driver too.
        *args: Positional arguments for ``func``.
        **kwargs: Keyword arguments for ``func``, all passed through.

    Returns:
        Result of ``func(*args, **kwargs)``.

    See Also:
        - ``docs/adr/0010-async-first-blocking-code.md`` — full rationale.
        - ``self.context.storage`` / ``self.context.state`` — already async.
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    # Auto-hold, unbounded (ADR-0018 → *Feeding the tracker*, mechanism 2).
    # `run_in_thread` is a mandatory seam, so this is the one place the SDK can
    # vouch for blocking work for free — and it must vouch without inventing a
    # duration. `func`'s own `timeout=` kwarg is not one: it is per-operation (a
    # `requests` read timeout bounds the gap between socket reads, a
    # `statement_timeout` bounds one statement of many), so a bound derived from
    # it is systematically *smaller* than the call's legitimate duration, and
    # the dominant shape — `run_in_thread(cursor.execute, sql)` — carries no
    # timeout at all. Any fallback tight enough to be useful would be tighter
    # than today's `start_to_close`, i.e. the upgrade would make legitimate long
    # blocking work fail sooner than before. So: unbounded, the watchdog is
    # inactive for the call's duration, the duration backstop owns it, and warn
    # mode reports every closed hold with its observed duration. That residual
    # is accepted and surfaced, not closed.
    call: Callable[[], T] = functools.partial(
        ctx.run, functools.partial(func, *args, **kwargs)
    )
    label = _offloaded_callable_name(func)
    scopes = _OFFLOAD_SCOPES.get()
    if scopes:
        call = _tracked_offload(call, scopes, label)
    cancel_handle = _bound_handle(func)
    with _auto_hold(_THREAD_HOLD_PREFIX + label, None):
        if cancel_handle is None:
            return await loop.run_in_executor(_BLOCKING_EXECUTOR, call)
        try:
            return await loop.run_in_executor(_BLOCKING_EXECUTOR, call)
        except asyncio.CancelledError:
            # Non-blocking: the action is handed to the cancel pool.
            cancel_handle.request()
            raise


#: The open :func:`tracking_offloads` scopes, innermost last. A tuple rather
#: than one set so a nested scope does not hide its calls from an enclosing one:
#: each ``run_in_thread`` records its marker in every open scope. Tasks copy the
#: context when they are created, so a ``TaskGroup`` opened inside a scope hands
#: the same sets to every child.
_OFFLOAD_SCOPES: contextvars.ContextVar[tuple[set["_OffloadMarker"], ...]] = (
    contextvars.ContextVar("_OFFLOAD_SCOPES", default=())
)


class _OffloadMarker(concurrent.futures.Future[None]):
    """A tracked call's marker, carrying the callable's name for the drain's log."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label


def _tracked_offload(
    call: Callable[[], T],
    scopes: tuple[set[_OffloadMarker], ...],
    label: str,
) -> Callable[[], T]:
    """Wrap *call* so the open scopes can see whether its thread is still running.

    The asyncio future ``run_in_executor`` returns cannot answer that: cancelling
    the awaiting task cancels it at once, while the thread it stood for runs on.
    The marker is a separate ``concurrent.futures.Future`` the thread itself
    drives — ``RUNNING`` for exactly as long as *call* is executing — so a drain
    waits on the work, not on the task that stopped waiting for it.
    """
    marker = _OffloadMarker(label)
    for pending in scopes:
        pending.add(marker)

    def _forget(done: "concurrent.futures.Future[None]") -> None:
        for pending in scopes:
            pending.discard(done)

    marker.add_done_callback(_forget)

    def _run() -> T:
        # A drain cancels markers still PENDING — calls queued behind a full
        # pool whose caller has already gone — so they never start, rather
        # than being waited on to run work nobody wants.
        if not marker.set_running_or_notify_cancel():
            raise concurrent.futures.CancelledError
        try:
            return call()
        finally:
            marker.set_result(None)

    return _run


@contextlib.contextmanager
def tracking_offloads() -> Iterator[set[_OffloadMarker]]:
    """Record every ``run_in_thread`` call made under this block, in any task.

    Yields the live set of markers for calls that have not finished; pass it to
    :func:`drain_offloads` to wait for them. Calls made in tasks created inside
    the block are recorded too, because a task inherits the context it was
    created in.
    """
    pending: set[_OffloadMarker] = set()
    token = _OFFLOAD_SCOPES.set((*_OFFLOAD_SCOPES.get(), pending))
    try:
        yield pending
    finally:
        _OFFLOAD_SCOPES.reset(token)


async def drain_offloads(
    pending: set[_OffloadMarker],
    *,
    max_no_progress_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until no thread recorded in *pending* is still running, or none is progressing.

    For the unwind path of a fan-out that was cancelled or failed: its tasks
    have stopped awaiting their threads, but the threads have not stopped, and
    whatever they write can land after the caller has moved on — a retry
    included. Calls still queued are cancelled so they never start.

    A cancellation arriving *during* the drain does not cut it short — that
    would reopen the gap being closed — but it is not lost: it is re-raised once
    the drain ends.

    **The drain waits only while it is getting somewhere.** It keeps its own
    stall clock, re-armed each time one of its threads finishes, and gives up
    once that clock reaches the no-progress allowance — the attempt's resolved
    ``max_no_progress_seconds``, the number the stall watchdog applies, or
    :data:`DEFAULT_MAX_NO_PROGRESS_SECONDS` outside an activity. This is
    enforced whatever ``ATLAN_PROGRESS_WATCHDOG`` says, because the alternative
    is not "wait a little longer": the calls drained here (an ``fsync``, a file
    read) take no timeout, so one wedged volume would hold the drain, the
    cancellation it is holding back, and the worker's shutdown behind it
    forever.

    It is deliberately not the attempt's own tracker. That one is re-armed by
    any work in the attempt and vouched for by every other offload's unbounded
    hold, so it cannot say whether *these* threads are moving; only their
    finishing can.

    Giving up is logged at WARNING with the calls still running, and then the
    drain returns — or re-raises the cancellation it was holding — so the
    caller's own failure propagates unchanged. Those threads are not stopped
    (a thread cannot be); a call that later completes can still write.

    Args:
        pending: The live marker set from :func:`tracking_offloads`.
        max_no_progress_seconds: Override for the allowance; tests use it.
        clock: Monotonic time source for the drain's stall clock.
    """
    # list() snapshots under the GIL; worker threads discard as they finish.
    running = [
        marker for marker in list(pending) if not marker.cancel() and not marker.done()
    ]
    if not running:
        return
    budget = max_no_progress_seconds
    if budget is None:
        budget = current_progress_tracker().max_no_progress_seconds
    if budget is None:
        budget = DEFAULT_MAX_NO_PROGRESS_SECONDS

    # Only a drained thread finishing is progress here; see the docstring.
    progress = ProgressTracker(clock=clock)
    waiting = {asyncio.wrap_future(marker): marker for marker in running}
    for marker in running:
        marker.add_done_callback(
            lambda _done, label=marker.label: progress.mark_progress(label)
        )

    cancelled = False
    while True:
        remaining = [w for w, marker in waiting.items() if not marker.done()]
        if not remaining:
            break
        quiet = progress.stalled_for()
        if quiet >= budget:
            logger.warning(
                "Stopped waiting for %d offloaded call(s) that made no progress "
                "for %.0fs: %s. They are still running and may write after the "
                "caller has moved on",
                len(remaining),
                quiet,
                ", ".join(sorted(waiting[w].label for w in remaining)),
            )
            break
        try:
            # Not wait_for: asyncio.wait never cancels what it waits on, so a
            # timeout or a cancellation here leaves the markers untouched.
            await asyncio.wait(
                remaining,
                timeout=budget - quiet,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def submit_in_thread(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> "concurrent.futures.Future[Any]":
    """Run a blocking *cleanup* call on the offload pool without awaiting it.

    ``run_in_thread`` is the primitive to reach for. This is its narrow
    companion for one specific problem: **cleanup that cancellation must not be
    able to skip.** An ``await`` inside a ``finally`` is itself a cancellation
    point, so a cancelled coroutine can be cut off before its own cleanup runs
    and leak whatever it was holding. Submitting instead of awaiting takes the
    event loop out of the path entirely: the call is handed to the pool and runs
    to completion whether or not the caller survives — and, because it is not on
    the loop, it may block on a lock held by an orphaned worker thread.

    Nothing awaits the result, so a failure has nowhere to surface. It is logged
    here rather than being swallowed by an unretrieved future.

    Use only for short, self-contained cleanup — closing a handle, releasing a
    lock — whose result no caller needs. Anything a caller must observe, or must
    know has finished before the next step, belongs in ``run_in_thread``.

    Args:
        func: Blocking cleanup callable.
        *args: Positional arguments for ``func``.
        **kwargs: Keyword arguments for ``func``.

    Returns:
        The pool future, for callers (in practice, tests) that want to wait on
        it. A cancelled future means the pool was already shut down and the call
        never ran. Ignoring the return value is the normal case.
    """
    ctx = contextvars.copy_context()
    try:
        future = _BLOCKING_EXECUTOR.submit(
            ctx.run, functools.partial(func, *args, **kwargs)
        )
    except RuntimeError:
        # The pool refuses work only once it has been shut down, which happens
        # at interpreter exit. Nothing is left running to clean up after, and the
        # OS reclaims whatever the process was holding — so this is ordinary
        # shutdown ordering rather than a failure, and a caller unwinding in a
        # ``finally`` must not have an exception raised at it here.
        logger.debug(
            "Offload pool is shut down; skipped detached cleanup call to %r",
            getattr(func, "__qualname__", func),
            exc_info=True,
        )
        skipped: concurrent.futures.Future[Any] = concurrent.futures.Future()
        skipped.cancel()
        return skipped
    future.add_done_callback(_report_detached_failure)
    return future


def _report_detached_failure(future: "concurrent.futures.Future[Any]") -> None:
    """Log a detached cleanup failure, since no caller will ever await it."""
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        logger.warning(
            "Detached cleanup call failed: %s: %s",
            type(error).__name__,
            error,
            exc_info=error,
        )


# Executor for work that must not be able to take the worker down with it.
#
# Why a process, not a thread?
#   A native fault (SIGSEGV in a C extension) is not a Python exception: it
#   bypasses every try/except and kills the whole process. In a thread that
#   means the Temporal worker dies mid-poll. In a child process the kernel
#   kills only the child, and the parent observes an ordinary, catchable
#   BrokenProcessPool.
#
# Why spawn, not fork?
#   fork() in a multi-threaded process (a Temporal worker always is) copies a
#   single thread but every lock, in whatever state the other threads left
#   them — a deadlock/corruption factory. spawn starts a clean interpreter.
#
# Lazy and cached by width: callers passing the same max_workers share one
# ProcessPoolExecutor of that width, created on first use; distinct widths get
# distinct pools, so discarding one width's pool on a crash never disturbs
# another's. Processes that never need isolation never pay for a child.
#
# Concurrency: the default pool runs several children so concurrent best-effort
# callers decode in PARALLEL, not serialised behind one child. Isolation here is
# purely fault containment — NOT a serialisation crutch to dodge the msgspec
# 0.20.0 concurrent-decode segfault: that bug is same-process (a shared
# in-process decoder across threads); separate child processes don't share that
# state, and once msgspec 0.21.1 lands even same-process concurrent decode is
# safe. The default width is capped low because each spawn child re-imports the
# decode stack (pyatlan/msgspec), which costs memory under a pod limit. A caller
# that wants to bound — or serialise (max_workers=1) — its own work passes an
# explicit width. (A crash in any one child breaks the whole ProcessPoolExecutor
# — inherent to the executor — so concurrent callers on that width then see
# BrokenProcessPool; for best-effort work that is a benign skip.)
_DEFAULT_PROCESS_POOL_MAX_WORKERS = min(4, (os.cpu_count() or 1))
_PROCESS_EXECUTORS: dict[int, concurrent.futures.ProcessPoolExecutor] = {}
_PROCESS_EXECUTORS_LOCK = threading.Lock()


def _get_process_executor(max_workers: int) -> concurrent.futures.ProcessPoolExecutor:
    with _PROCESS_EXECUTORS_LOCK:
        executor = _PROCESS_EXECUTORS.get(max_workers)
        if executor is None:
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
            _PROCESS_EXECUTORS[max_workers] = executor
        return executor


def _discard_process_executor(max_workers: int) -> None:
    """Drop the width-``max_workers`` pool (and kill its children) so the next
    call at that width starts fresh."""
    with _PROCESS_EXECUTORS_LOCK:
        executor = _PROCESS_EXECUTORS.pop(max_workers, None)
    if executor is None:
        return
    # Kill the children BEFORE shutdown(): a dead child is the pool's
    # well-trodden unwind path — the manager thread sees it, marks the pool
    # broken, resolves every still-queued work item with BrokenProcessPool,
    # and every internal thread exits. The reverse order (shutdown, then kill)
    # strands a manager/feeder thread on a lock and hangs interpreter exit.
    # No cancel_futures=True: cancelling a *foreign* caller's queued future
    # would surface as CancelledError (a BaseException) in that innocent
    # caller; the broken-pool resolution reaches it as a catchable
    # BrokenProcessPool instead. shutdown() never kills a *running* child
    # (e.g. one hung past a timeout) and ProcessPoolExecutor exposes no
    # supported kill, so reach for the internal process table (None once the
    # pool is broken); on a future CPython that renames it, the child leaks
    # until it finishes — degraded, not fatal.
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        process.kill()
    executor.shutdown(wait=False)


async def run_fault_isolated(
    func: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    max_workers: int | None = None,
    **kwargs: Any,
) -> T:
    """Run ``func`` in an isolated child process (native-crash containment).

    The mechanism layer. Unlike :func:`run_in_thread`, this survives faults that
    are not Python exceptions: if ``func`` segfaults a C extension, only the
    child dies and the caller gets a catchable :class:`BrokenProcessPool`. Use it
    for work whose native fault must never take the worker process down.

    This *raises* on failure (``BrokenProcessPool`` / ``TimeoutError``) — the
    caller decides what to do. For non-essential work that should be silently
    skipped on failure, prefer :func:`run_best_effort`, which wraps this and
    swallows failures. Essential work — where a failure should fail the activity
    — should not be isolated per-call at all; run it in-process or via
    :func:`run_in_thread` and let Temporal/k8s recover a crash.

    Constraints that :func:`run_in_thread` does not have:

    - ``func``, ``args``, ``kwargs`` and the return value must be picklable;
      ``func`` must be a module-level function (pickled by reference).
    - ContextVars do **not** propagate — the child is a fresh interpreter.
      Have the child return data and log from the parent.
    - The child imports ``func``'s module on first use (one-time cost,
      amortized by the pooled worker).

    Like :func:`run_in_thread`, the call is automatically wrapped in a progress
    hold (ADR-0018) so an isolated call — which emits nothing at all until the
    child returns — is never read as a stall. Here the hold is *bounded* by
    ``timeout`` when one is given, because that number is already a wall-clock
    kill enforced below; ``timeout=None`` gives an unbounded hold, matching its
    "wait forever" semantics.

    Args:
        func: Module-level function to run in the child.
        timeout: Seconds to wait before killing the child and raising
            ``TimeoutError``. ``None`` waits forever.
        max_workers: Width of the (width-keyed) process pool this call runs on.
            ``None`` (default) uses ``min(4, cpu_count)`` so concurrent callers
            decode in parallel. Pass ``1`` to opt into sequential execution —
            all ``max_workers=1`` callers share a single child and queue behind
            one another; pass another integer to bound concurrency at a
            different width. Must be >= 1.

    Raises:
        BrokenProcessPool: The child died abnormally (native crash), or a
            concurrent caller on the same pool discarded it (timeout) while this
            call was in flight. That pool is discarded; the next call at this
            width gets a fresh child.
        TimeoutError: ``timeout`` elapsed. The child is killed and the pool
            discarded.
        InvalidInputValueError: ``max_workers`` is < 1 (also a ``ValueError``).
    """
    workers = _DEFAULT_PROCESS_POOL_MAX_WORKERS if max_workers is None else max_workers
    if workers < 1:
        raise InvalidInputValueError(
            message=f"max_workers must be >= 1, got {workers}",
            field="max_workers",
            constraint=">= 1",
            value_summary=str(workers),
        )
    loop = asyncio.get_running_loop()
    # Auto-hold, bounded by this call's own `timeout` (ADR-0018 → *Feeding the
    # tracker*, mechanism 2 — the same treatment as `run_in_thread`, since this
    # is the SDK's other offload seam and an isolated call emits nothing at all
    # until the child returns; the SDK's own default `timeout` for the upload
    # validation scan is 600s, well past any plausible no-progress budget).
    #
    # Bounded rather than unbounded because unlike `run_in_thread` there is a
    # real wall-clock number here and it belongs to *this* function, not to
    # `func`: the block below enforces `timeout` by killing the child and raising
    # `TimeoutError`, so it means precisely what `enter_hold`'s allowance means —
    # how long the caller would let this one operation run before it would rather
    # it failed. What ADR-0018 rejects is deriving a bound from the *callee's*
    # per-operation kwargs, which is a different number. `timeout=None` still
    # means an unbounded hold, matching `None`'s "wait forever" here.
    #
    # Inside a `holding_progress` block this stands down like `run_in_thread`'s
    # does — see `_auto_hold`. Harmless even when this call's own `timeout` is
    # tighter than the declared allowance: `timeout` is enforced here by killing
    # the child, so the call returns at its deadline whether or not a hold is
    # watching.
    with _auto_hold(_ISOLATED_HOLD_PREFIX + _offloaded_callable_name(func), timeout):
        future = loop.run_in_executor(
            _get_process_executor(workers), functools.partial(func, *args, **kwargs)
        )
        if timeout is not None:
            # Not asyncio.wait_for: on timeout it cancels the future and then
            # waits for the cancellation to land — but a running executor call
            # cannot be cancelled, so wait_for would hang exactly when the child
            # hangs. asyncio.wait just stops waiting; we then kill the child
            # ourselves.
            done, _ = await asyncio.wait({future}, timeout=timeout)
            if not done:
                _discard_process_executor(workers)
                # The kill resolves the abandoned future with BrokenProcessPool;
                # consume it so asyncio never logs "exception was never
                # retrieved".
                future.add_done_callback(
                    lambda f: None if f.cancelled() else f.exception()
                )
                raise TimeoutError(f"run_fault_isolated timed out after {timeout}s")
        try:
            return await future
        except BrokenProcessPool:
            _discard_process_executor(workers)
            raise
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # real cancellation of the caller — must propagate
            # Foreign cancellation: a concurrent caller's timeout discarded the
            # shared pool while this call was still queued. From this caller's
            # perspective that is exactly a broken pool — surface it as the
            # catchable exception the contract promises, never CancelledError.
            raise BrokenProcessPool(
                "process pool was discarded while this call was queued"
            ) from None


async def run_best_effort(
    func: Callable[..., T],
    *args: Any,
    label: str,
    logger: AtlanLoggerAdapter,
    timeout: float | None = None,
    max_workers: int | None = None,
    **kwargs: Any,
) -> T | None:
    """Run non-essential native work fault-isolated; never let it break the caller.

    The policy layer over :func:`run_fault_isolated`. Runs ``func`` in an
    isolated child process and, on *any* failure — a native crash
    (``BrokenProcessPool``), a ``timeout``, or an ordinary exception — logs a
    warning via ``logger`` and returns ``None`` rather than propagating. This is
    the SDK's sanctioned home for *best-effort* native work: work whose result is
    used when present and safely skipped when absent, and which must never crash
    or fail the worker (e.g. the warn-only upload validation scan). Essential
    work — where a failure *should* fail the activity — must not use this.

    A genuine caller cancellation (``asyncio.CancelledError`` from cooperative
    task cancellation) is deliberately **not** swallowed — it propagates.

    Args:
        func: Module-level function to run in the child. Same picklability /
            ContextVar constraints as :func:`run_fault_isolated`.
        label: Human label for the work, interpolated into the warning
            (e.g. ``"Transformed-asset validation"``).
        logger: The caller's logger, so the warning is attributed to the
            caller's module (OTel source) rather than this one.
        timeout: Seconds before the child is killed and the run is skipped.
        max_workers: Pool width, forwarded to :func:`run_fault_isolated`. ``None``
            uses the default ``min(4, cpu_count)``; pass ``1`` to serialise this
            work.

    Returns:
        ``func``'s result, or ``None`` if the run crashed, timed out, or errored.
    """
    try:
        return await run_fault_isolated(
            func, *args, timeout=timeout, max_workers=max_workers, **kwargs
        )
    except BrokenProcessPool:
        logger.warning(
            "%s subprocess died or was discarded (a native fault in a "
            "dependency, or a concurrent call's timeout); continuing without it",
            label,
            exc_info=True,
        )
    except TimeoutError:
        logger.warning(
            "%s timed out after %ss; continuing without it",
            label,
            timeout,
            exc_info=True,
        )
    except Exception:  # noqa: BLE001 — best-effort work must never break the caller
        logger.warning("%s skipped due to an unexpected error", label, exc_info=True)
    return None
