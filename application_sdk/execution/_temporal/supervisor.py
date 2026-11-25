"""Restart supervisor for the Temporal worker, and the poll-state observer beside it.

Moved out of ``application_sdk.main`` (FND-4689). ``main`` calls
:func:`_run_worker_with_restart` from the worker and combined run modes and
builds a :class:`_FatalTeardownWatchdog` for it; nothing else imports this
module.

It lives under ``execution/_temporal`` rather than ``_runtime`` because every
dependency it has sits at this layer: it rebuilds a temporalio ``Worker``, reads
``execution.settings`` for the teardown bound, and reads sdk-core's poller
gauge through ``execution._temporal``. ``_runtime`` is the dependency-neutral
substrate (ADR-0019) and may import none of those.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from application_sdk.common._env import env_int as _env_int
from application_sdk.common.backoff import Backoff
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from temporalio.client import Client

    from application_sdk.execution._temporal.auth import TemporalAuthManager
    from application_sdk.server.health import WorkerHealthServer

logger = get_logger(__name__)


# --- Worker restart supervisor ----------------------------------------------
# A transient poll auth failure — e.g. a JWKS signing-key cache skew on the
# Temporal frontend that rejects an otherwise-valid token for a few seconds —
# makes temporalio treat the poll as fatal and the worker process exits. Without
# a supervisor the whole runtime goes inactive until a manual restart. These
# bounds drive an automatic rebuild-and-restart of the worker, while still
# failing loud when the failure is persistent (e.g. genuinely bad credentials).
_WORKER_MAX_CONSECUTIVE_RESTARTS = _env_int("ATLAN_WORKER_MAX_CONSECUTIVE_RESTARTS", 10)
_WORKER_RESTART_BACKOFF_CAP_SECONDS = _env_int(
    "ATLAN_WORKER_RESTART_BACKOFF_CAP_SECONDS", 30
)
_WORKER_HEALTHY_RUN_SECONDS = _env_int("ATLAN_WORKER_HEALTHY_RUN_SECONDS", 300)
# Poll-state diagnostics (ARUN-1127). How often to read sdk-core's live poller
# gauge and log a transition. Purely observational — set to 0 to disable.
_WORKER_POLL_DIAGNOSTIC_INTERVAL_SECONDS = _env_int(
    "ATLAN_WORKER_POLL_DIAGNOSTIC_INTERVAL_SECONDS", 60
)
# Consecutive definitive zero-poller readings before this worker stops emitting
# token_refresh health events. At the default 60s observer interval that is ~3
# minutes of confirmed-parked polling — long enough that a blip or a slow start
# never trips it, short enough that a parked worker stops advertising itself as
# healthy well inside a sync window.
_WORKER_ZERO_POLLER_READINGS_BEFORE_STALE = _env_int(
    "ATLAN_WORKER_ZERO_POLLER_READINGS_BEFORE_STALE", 3
)
# Upper bound on temporalio's own worker teardown after a fatal poll error.
# The teardown drains in-flight activities for up to graceful_shutdown_timeout,
# so it should never take much longer than that. Unset, the bound is that
# timeout plus a margin; 0 or negative disables the bound.
_WORKER_FATAL_TEARDOWN_TIMEOUT_ENV = "ATLAN_WORKER_FATAL_TEARDOWN_TIMEOUT_SECONDS"
_WORKER_FATAL_TEARDOWN_MARGIN_SECONDS = 60
_WORKER_FATAL_TEARDOWN_EXIT_CODE = 1


class _FatalTeardownWatchdog:
    """Exits the process when a worker's teardown after a fatal error never ends.

    The restart supervisor regains control only when ``Worker.__aexit__``
    returns. After a fatal poll error that teardown has been seen to never
    finish: the process stays up with a refreshing token and green probes,
    nothing polls the task queue, and no restart policy fires because nothing
    exited. Temporal's ``on_fatal_error`` hook still fires first, so it arms
    this watchdog, and the supervisor disarms it as soon as the teardown
    returns. If the bound passes first, the process exits non-zero and the pod
    or container restart policy brings up a fresh worker — the one recovery
    that works from inside a wedged teardown.

    The timer runs on a daemon thread so it fires even if the event loop is
    blocked, and ``os._exit`` is used because a clean interpreter shutdown
    would wait on the very teardown that is stuck.
    """

    def __init__(
        self,
        timeout_seconds: float,
        exit_fn: Callable[[int], Any] = os._exit,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._exit_fn = exit_fn
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        # Bumped on every arm so a callback can tell whether its own arm is
        # still the live one; see _expire.
        self._generation = 0

    @classmethod
    def from_settings(cls) -> _FatalTeardownWatchdog:
        from application_sdk.execution.settings import (  # noqa: PLC0415 — cold path: worker startup only
            load_execution_settings,
        )

        default = (
            load_execution_settings().graceful_shutdown_timeout_seconds
            + _WORKER_FATAL_TEARDOWN_MARGIN_SECONDS
        )
        return cls(_env_int(_WORKER_FATAL_TEARDOWN_TIMEOUT_ENV, default))

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._timer is not None

    def arm(self) -> None:
        """Start the bound. A second fatal before disarm keeps the first deadline."""
        if self._timeout_seconds <= 0:
            return
        with self._lock:
            if self._timer is not None:
                return
            self._generation += 1
            self._timer = threading.Timer(
                self._timeout_seconds, self._expire, args=(self._generation,)
            )
            self._timer.daemon = True
            self._timer.start()

    def disarm(self) -> None:
        """The teardown returned; the supervisor has control again."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _expire(self, generation: int) -> None:
        # Timer.cancel() cannot stop a callback that has already started, so a
        # teardown returning right at the bound can disarm while this runs.
        # Re-check under the lock and hold it through the exit, so a disarm
        # either lands first (and this returns) or blocks until the process is
        # gone — never "disarmed, then exited anyway".
        with self._lock:
            if self._timer is None or generation != self._generation:
                return
            logger.error(
                "Temporal worker teardown did not finish within %ds of a fatal "
                "error; exiting with code %d so the process is restarted",
                self._timeout_seconds,
                _WORKER_FATAL_TEARDOWN_EXIT_CODE,
            )
            self._exit_fn(_WORKER_FATAL_TEARDOWN_EXIT_CODE)


async def _observe_worker_poll_state(
    *,
    shutdown_event: asyncio.Event,
    health_server: WorkerHealthServer | None = None,
    interval: float = 0,
) -> None:
    """Log whether this worker is actually polling, and feed the ``/live`` probe.

    A worker can be alive, healthy on every probe, refreshing its Temporal token
    — and have zero pollers on its task queue, doing no work until a human
    restarts the pod (ARUN-1127 / BLDX-1552). Two candidate mechanisms produce
    that shape and they need opposite fixes:

    1. A fatal that was raised and then swallowed. ``Worker.__aexit__`` re-raises
       a captured fatal only when the body exits via ``CancelledError``, so a
       fatal racing a shutdown is dropped. Temporal's ``on_fatal_error`` hook
       (wired in ``create_worker``) always sees it, so this case leaves a
       ``poll loop failed fatally`` line in the logs.
    2. A poll loop that stopped without ever raising. No exception exists, so
       nothing in-process can be made to observe it after the fact.

    This observer records the evidence that tells them apart — sdk-core's live
    poller gauge alongside the fatal count. It never raises and never restarts
    the worker in-process: a remedy built on the wrong mechanism either tears
    down healthy workers or hides the outage behind a self-healing loop that
    cannot heal it. Instead the readings go to the health server, whose ``/live``
    probe fails on a sustained definitive zero or an unrecovered fatal
    (FND-4641), so the pod restart policy — which works whichever mechanism
    applies — does the recovery.

    Logs a transition rather than every reading, so a long-running worker stays
    quiet until something changes: an active-pollers reading is an INFO on change,
    and a zero or unknown reading is a WARNING logged once per transition into
    that state (not on every interval, so a parked worker does not drown the
    ``poll loop failed fatally`` line in repeats).

    Args:
        shutdown_event: Set on SIGINT/SIGTERM; ends the observer cleanly.
        health_server: Optional ``WorkerHealthServer`` to record readings on, so
            the counts are also reachable over ``/live`` and ``/ready``.
        interval: Seconds between readings. Non-positive disables the observer.
    """
    if interval <= 0:
        logger.debug("Worker poll-state diagnostics disabled (interval=%s)", interval)
        return

    from application_sdk.execution._temporal.poll_state import (  # noqa: PLC0415 — cold path: diagnostics only
        worker_poll_state,
    )
    from application_sdk.execution._temporal.worker import (  # noqa: PLC0415 — cold path: diagnostics only
        read_core_poller_counts,
    )

    worker_poll_state.configure(
        zero_readings_before_stale=_WORKER_ZERO_POLLER_READINGS_BEFORE_STALE
    )

    # Sentinel distinct from every real state, so the first reading always logs.
    previous_state: str | None = None

    while not shutdown_event.is_set():
        # Wait first: core registers the gauge only once a worker has polled at
        # least once, so a reading taken at t=0 always reports "unknown".
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except TimeoutError:  # conformance: ignore[E002] the interval elapsing without a shutdown is the expected path, not an error
            pass

        try:
            counts = await read_core_poller_counts()
            if health_server is not None:
                health_server.record_poller_counts(counts)

            if counts is None:
                state = "unknown"
            elif sum(counts.values()) > 0:
                state = "polling"
            else:
                state = "zero"

            if state == "zero":
                # Transition-gated like "unknown" below: warn once when the
                # worker parks, not on every interval, so a parked worker does
                # not bury the "poll loop failed fatally" line this PR exists
                # to surface. A return to "polling" and back re-arms it.
                if previous_state != "zero":
                    logger.warning(
                        "Worker reports 0 active pollers (poller gauge %s) — it is "
                        "alive but claiming no work from its task queue; check the "
                        "logs for a 'poll loop failed fatally' line, whose presence "
                        "or absence identifies the failure mechanism (ARUN-1127)",
                        counts,
                    )
            elif state == "unknown":
                if previous_state != "unknown":
                    logger.warning(
                        "Worker poll-state unknown: sdk-core's poller gauge could "
                        "not be read, so a parked poll loop would be invisible. "
                        "Check that core's Prometheus exporter is enabled "
                        "(ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS)"
                    )
            elif state != previous_state:
                logger.info("Worker poll state: active pollers %s", counts)

            # Gates token_refresh health events. Only a sustained, definitive
            # "zero" withholds them; "polling" and "unknown" both fail open.
            worker_poll_state.record(state)

            previous_state = state
        except Exception:
            # An observer must never take the worker down with it. A failed
            # reading is not evidence that the worker is dead, so reopen the
            # gate rather than leaving a prior suppression in place.
            logger.warning("Worker poll-state observation failed", exc_info=True)
            worker_poll_state.record("unknown")


async def _run_worker_with_restart(
    *,
    build_worker: Callable[[], Any],
    shutdown_event: asyncio.Event,
    auth_manager: TemporalAuthManager | None = None,
    client: Client | None = None,
    health_server: WorkerHealthServer | None = None,
    reconnect: Callable[[], Awaitable[None]] | None = None,
    teardown_watchdog: _FatalTeardownWatchdog | None = None,
) -> None:
    """Run a Temporal worker under a bounded restart supervisor.

    temporalio treats a poll ``PermissionDenied`` (and other non-retryable gRPC
    errors) as fatal: the worker shuts down and re-raises out of the
    ``async with worker`` block. A ``Worker`` is single-use, so each restart
    rebuilds one via ``build_worker``.

    Restart-on-fatal with a cap: any worker-fatal error triggers a rebuild after
    a full-jitter backoff, unless a shutdown was requested. The consecutive
    counter resets once a worker runs healthily for ``_WORKER_HEALTHY_RUN_SECONDS``;
    if the worker keeps failing without ever staying up, the supervisor gives up
    after ``_WORKER_MAX_CONSECUTIVE_RESTARTS`` and re-raises so a persistent
    misconfiguration still fails loud rather than hot-looping forever.

    Args:
        build_worker: Builds a fresh worker (an ``AppWorker``) to run.
        shutdown_event: Set on SIGINT/SIGTERM; a clean shutdown stops the loop.
        auth_manager: Optional; when present its token is force-refreshed before
            each restart to recover from stale/expired tokens.
        client: The Temporal client passed to ``auth_manager.force_refresh``.
        health_server: Optional ``WorkerHealthServer``; receives the poll-state
            observer's readings so they are also reachable over the probes.
        reconnect: Optional; rebuilds the Temporal client before each restart.
            Preferred over ``force_refresh`` — see ``_supervise_worker``.
        teardown_watchdog: Optional; armed by the worker's ``on_fatal_error``
            hook and disarmed here once the teardown returns. Bounds a teardown
            that never finishes — see ``_FatalTeardownWatchdog``.
    """
    # Runs for the whole supervised lifetime, across worker rebuilds, so a
    # rebuilt worker that comes back without pollers is still observed. It never
    # raises and never restarts anything (its readings reach /live via the
    # health server), so it is not raced against the worker.
    observer = asyncio.ensure_future(
        _observe_worker_poll_state(
            shutdown_event=shutdown_event,
            health_server=health_server,
            interval=_WORKER_POLL_DIAGNOSTIC_INTERVAL_SECONDS,
        )
    )
    try:
        await _supervise_worker(
            build_worker=build_worker,
            shutdown_event=shutdown_event,
            auth_manager=auth_manager,
            client=client,
            reconnect=reconnect,
            teardown_watchdog=teardown_watchdog,
        )
    finally:
        observer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await observer
        # The supervised lifetime is over (shutdown, or the cap re-raising), so
        # reopen the health-event gate. Nothing should stay muted once the
        # observer that muted it has stopped reading.
        from application_sdk.execution._temporal.poll_state import (  # noqa: PLC0415 — cold path: shutdown only
            worker_poll_state,
        )

        worker_poll_state.reset()


async def _supervise_worker(
    *,
    build_worker: Callable[[], Any],
    shutdown_event: asyncio.Event,
    auth_manager: TemporalAuthManager | None = None,
    client: Client | None = None,
    reconnect: Callable[[], Awaitable[None]] | None = None,
    teardown_watchdog: _FatalTeardownWatchdog | None = None,
) -> None:
    """The restart loop itself; see ``_run_worker_with_restart`` for the contract."""
    consecutive_failures = 0

    while not shutdown_event.is_set():
        worker = build_worker()
        started_at = time.monotonic()
        try:
            try:
                async with worker:
                    await shutdown_event.wait()
            finally:
                # Reaching here at all means the teardown returned, so a bound
                # armed by a fatal error has done its job.
                if teardown_watchdog is not None:
                    teardown_watchdog.disarm()
            # Body returned normally — a shutdown signal, not a failure. Stop.
            return
        except Exception:
            # temporalio surfaces a worker-fatal error by cancelling this task
            # and re-raising the error from Worker.__aexit__. Clear the residual
            # cancellation so the backoff below (asyncio.wait_for) isn't torn
            # down by the leftover cancel count on Python 3.11+.
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()

            if shutdown_event.is_set():
                # Failure raced with an in-flight shutdown — treat as clean.
                logger.info("Worker exited during shutdown; not restarting")
                return

            ran_seconds = time.monotonic() - started_at
            if ran_seconds >= _WORKER_HEALTHY_RUN_SECONDS:
                # Ran healthily for a while before failing — a fresh incident,
                # not a restart storm, so reset the streak.
                consecutive_failures = 0
            consecutive_failures += 1

            if consecutive_failures > _WORKER_MAX_CONSECUTIVE_RESTARTS:
                logger.error(
                    "Worker failed %d times without staying healthy for %ds; "
                    "giving up and exiting",
                    consecutive_failures,
                    _WORKER_HEALTHY_RUN_SECONDS,
                    exc_info=True,
                )
                raise

            # Rebuild the client before restarting, when the caller wired a
            # reconnect. A fresh token on the same channel is not enough: the
            # rebuilt worker registers and emits worker_start but never resumes
            # polling, and since run() then neither returns nor raises, this
            # loop never sees a second failure — the process stays alive with
            # green probes and a refreshing token, doing nothing. Reconnecting
            # is what a pod restart does, and a pod restart is what recovers it.
            if reconnect is not None:
                try:
                    await reconnect()
                except Exception:
                    logger.warning(
                        "Reconnect before worker restart failed; restarting anyway",
                        exc_info=True,
                    )
            elif auth_manager is not None and client is not None:
                try:
                    await auth_manager.force_refresh(client)
                except Exception:
                    logger.warning(
                        "Token refresh before worker restart failed; restarting anyway",
                        exc_info=True,
                    )

            # Full-jitter exponential backoff from the shared Backoff, raced
            # against shutdown so SIGTERM stays responsive during backoff. This
            # loop keeps its own control flow (the streak reset above, the
            # shutdown race below), so it takes only the delay. The cap is
            # read per restart because it is env-overridable.
            delay = Backoff(base=1.0, cap=_WORKER_RESTART_BACKOFF_CAP_SECONDS).delay(
                consecutive_failures
            )
            logger.warning(
                "Worker exited with a fatal error (attempt %d/%d, ran %.0fs); "
                "restarting in %.1fs",
                consecutive_failures,
                _WORKER_MAX_CONSECUTIVE_RESTARTS,
                ran_seconds,
                delay,
                exc_info=True,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
                # Shutdown requested during backoff — stop.
                return
            except TimeoutError:
                # Backoff elapsed without a shutdown request — the expected
                # path; fall through to rebuild and restart the worker.
                logger.debug("Restart backoff elapsed; rebuilding worker")
