"""Shutdown policy for a worker process: the post-drain exit deadline and the
event-loop stall bound.

Moved out of ``application_sdk.main`` (FND-4689). The mechanisms live
elsewhere — :mod:`application_sdk._loop_stall_watchdog` is the stall watchdog
itself — and this module derives the numbers they run with from the shutdown
budget (``ATLAN_SHUTDOWN_GRACE_SECONDS``) and the registered tasks, checks that
those numbers are in order, and logs them once at startup. ``main``'s worker
and combined run modes are the only callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from application_sdk._loop_stall_watchdog import (
    LOOP_STALL_TIMEOUT_ENV,
    LoopStallWatchdog,
    largest_bound_within,
)
from application_sdk.app.registry import TaskRegistry
from application_sdk.common._env import env_int as _env_int
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# Upper bound on what runs after the worker has drained on a shutdown signal:
# the worker wrapper's cleanup (credential directories, sizing rows, the
# Pushgateway final push, hold and DELETE), closing infrastructure, flushing
# telemetry and the interpreter's exit hooks. No activity is in flight by then,
# yet these steps have been seen to block for the whole pod grace period when a
# telemetry endpoint is unreachable, holding the pod's requests and its node.
# 0 or negative disables the bound. Unset, it is derived from
# ATLAN_SHUTDOWN_GRACE_SECONDS when that is set (ShutdownBudget), else 120.
# The exit code is non-zero and distinct (1 is the fatal-teardown watchdog, 2 is
# argparse) so a cut-short shutdown shows in the container's last exit code
# instead of passing as a clean stop.
_POST_DRAIN_EXIT_TIMEOUT_ENV = "ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS"
_POST_DRAIN_EXIT_TIMEOUT_DEFAULT_SECONDS = 120
_POST_DRAIN_EXIT_CODE = 3


def _post_drain_exit_timeout_seconds() -> int:
    """Explicit env override, else the grace-derived bound, else 120 s."""
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: shutdown only
        load_shutdown_budget,
    )

    budget = load_shutdown_budget()
    default = (
        budget.post_drain_seconds
        if budget is not None
        else _POST_DRAIN_EXIT_TIMEOUT_DEFAULT_SECONDS
    )
    return _env_int(_POST_DRAIN_EXIT_TIMEOUT_ENV, default)


class _PostDrainExitArm:
    """Starts the post-drain exit deadline at most once, and only on shutdown.

    Armed from the worker's ``on_drained`` hook the moment the Temporal drain
    returns — so the worker wrapper's own cleanup, including the Pushgateway's
    pre-DELETE hold and two HTTP calls, runs under the deadline — and again
    after the supervisor returns, as a fallback for a stop that never reached
    the hook. A worker that stops without a shutdown signal (a supervised
    restart after a fatal error) never arms it.

    Starting it also stops ``stall_watchdog``: from here on the post-drain
    deadline alone bounds the shutdown, as it does when no watchdog runs.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        shutdown_event: asyncio.Event,
        stall_watchdog: LoopStallWatchdog | None = None,
    ) -> None:
        self._enabled = enabled
        self._shutdown_event = shutdown_event
        self._stall_watchdog = stall_watchdog
        self._lock = threading.Lock()
        self._started = False

    def on_worker_drained(self) -> None:
        if self._shutdown_event.is_set():
            self.start()

    def start(self) -> None:
        if self._stall_watchdog is not None:
            self._stall_watchdog.stop()
        if not self._enabled:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        _start_post_drain_exit_deadline()


def _log_shutdown_budget(loop_stall: _LoopStallBound | None = None) -> None:
    """Log the effective shutdown bounds once, and warn when they overrun the grace.

    Every bound has its own env override, so the effective values can disagree
    with the declared grace. A sum past the grace means SIGKILL lands before
    shutdown finishes; logging it at startup makes that visible before it
    happens instead of after. The event-loop stall bound is checked against
    its own ordering: above the longest heartbeat timeout, and within the
    budget.
    """
    from application_sdk.app.task import (  # noqa: PLC0415 — cold path: worker startup only
        HEARTBEAT_TIMEOUT_ENV,
    )
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: worker startup only
        PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS,
        PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS,
        SHUTDOWN_DRAIN_DELAY_SECONDS,
        SHUTDOWN_GRACE_SECONDS_ENV,
        load_shutdown_budget,
    )
    from application_sdk.execution.settings import (  # noqa: PLC0415 — cold path: worker startup only
        load_execution_settings,
    )

    budget = load_shutdown_budget()
    drain = load_execution_settings().graceful_shutdown_timeout_seconds
    post_drain = _post_drain_exit_timeout_seconds()
    logger.info(
        "Shutdown budget: grace=%s drain_delay=%ss drain=%ss post_drain=%ss "
        "pushgateway_http_timeout=%ss pushgateway_delete_delay=%ss loop_stall=%s",
        budget.grace_seconds if budget is not None else "undeclared",
        SHUTDOWN_DRAIN_DELAY_SECONDS,
        drain,
        post_drain,
        PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS,
        PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS,
        f"{loop_stall.seconds:g}s"
        if loop_stall is not None and loop_stall.seconds > 0
        else "off",
    )
    if loop_stall is not None and not loop_stall.in_order():
        if not loop_stall.overridden and loop_stall.heartbeat_task is None:
            # Only the SDK's default heartbeat timeout is past what the grace
            # allows: expected for a short grace, and nothing in the app to fix.
            logger.info(
                "Event-loop stall bound %gs is below the default heartbeat timeout "
                "(%gs, %s) because %s=%s leaves no more room before SIGKILL; a "
                "task holding the event loop longer than that during the drain "
                "is ended as a stall",
                loop_stall.seconds,
                loop_stall.longest_heartbeat_seconds,
                HEARTBEAT_TIMEOUT_ENV,
                SHUTDOWN_GRACE_SECONDS_ENV,
                budget.grace_seconds if budget is not None else "undeclared",
            )
        else:
            logger.warning(
                "Event-loop stall bound out of order: the longest registered "
                "heartbeat timeout (%gs) < loop_stall %gs <= what still fires "
                "before SIGKILL (%s) must hold. %s",
                loop_stall.longest_heartbeat_seconds,
                loop_stall.seconds,
                f"{loop_stall.limit_seconds:g}s"
                if loop_stall.limit_seconds is not None
                else "unbounded",
                _loop_stall_order_advice(loop_stall),
            )
    if loop_stall is not None and loop_stall.unwatched_tasks:
        logger.warning(
            "Tasks without a heartbeat timeout may run past the event-loop stall "
            "bound (loop_stall %gs): %s. If one holds the event loop during "
            "shutdown, the watchdog ends the process after %gs instead of waiting "
            "for it. Give them a heartbeat timeout to make the wait explicit",
            loop_stall.seconds,
            ", ".join(loop_stall.unwatched_tasks),
            loop_stall.seconds,
        )
    if budget is None:
        return
    # A disabled post-drain deadline (<= 0) is unbounded; count the derived
    # value so the check still reflects what the grace was split for.
    total = (
        SHUTDOWN_DRAIN_DELAY_SECONDS
        + drain
        + (post_drain if post_drain > 0 else budget.post_drain_seconds)
        # Kept back for daprd's stop after the process exits; eating into it
        # leaves the sidecar to be SIGKILLed.
        + budget.headroom_seconds
    )
    if total > budget.grace_seconds:
        logger.warning(
            "Shutdown bounds overrun the pod grace period: drain_delay %ss + "
            "drain %ss + post_drain %ss + daprd headroom %ss = %ss > %s=%s; "
            "SIGKILL will land before shutdown finishes. Unset the explicit "
            "overrides to derive them from the grace",
            SHUTDOWN_DRAIN_DELAY_SECONDS,
            drain,
            post_drain,
            budget.headroom_seconds,
            total,
            SHUTDOWN_GRACE_SECONDS_ENV,
            budget.grace_seconds,
        )


def _start_post_drain_exit_deadline(
    exit_fn: Callable[[int], Any] = os._exit,
) -> threading.Timer | None:
    """Exit the process if shutdown has not finished soon after the worker drained.

    Started only once the worker has returned from its drain, so cutting the
    remaining teardown short cannot interrupt an activity. It ends the whole
    process, so only the process-owning entry point (``run_main``) turns it on;
    a caller that awaits a run mode directly and keeps the process gets no
    timer. The timer is never
    cancelled: it also bounds the interpreter's exit hooks, and on a normal exit
    it dies with the process. It writes to stderr directly because the logging
    pipeline is one of the things that can be stuck.
    """
    timeout = _post_drain_exit_timeout_seconds()
    if timeout <= 0:
        return None

    def _expire() -> None:
        # Best effort: a closed or broken stderr must not stop the exit.
        with contextlib.suppress(OSError):
            os.write(
                2,
                f"Shutdown did not finish within {timeout}s of the worker draining; "
                f"exiting with code {_POST_DRAIN_EXIT_CODE}\n".encode(),
            )
        exit_fn(_POST_DRAIN_EXIT_CODE)

    timer = threading.Timer(timeout, _expire)
    timer.daemon = True
    timer.start()
    return timer


# Kept between the longest heartbeat timeout and the stall bound, so a task
# that holds the loop for exactly its heartbeat timeout is never cut short.
_LOOP_STALL_MARGIN_SECONDS = 15


@dataclass(frozen=True)
class _LoopStallBound:
    """The event-loop stall bound for a shutting-down worker, and its inputs."""

    seconds: float
    """The bound; ``0`` turns the watchdog off."""
    longest_heartbeat_seconds: float
    """The longest heartbeat timeout among the registered tasks."""
    limit_seconds: float | None
    """The largest bound that still ends the process before SIGKILL; ``None``
    when the grace is not declared."""
    overridden: bool
    unwatched_tasks: tuple[str, ...] = ()
    """App tasks without a heartbeat timeout that may run longer than the bound,
    as ``app.task (start-to-close s)``."""
    heartbeat_task: str | None = None
    """The task whose heartbeat timeout is the longest, as ``app.task``, when it
    is longer than the SDK's default; ``None`` when the default is the longest."""
    exit_by_seconds: float | None = None
    """How long after the signal the watchdog's exit must land so daprd keeps
    its headroom; ``None`` when the grace is not declared."""

    def in_order(self) -> bool:
        """``longest heartbeat timeout < bound <= limit``, or the watchdog is off."""
        if self.seconds <= 0:
            return True
        above_heartbeats = self.seconds > self.longest_heartbeat_seconds
        return above_heartbeats and (
            self.limit_seconds is None or self.seconds <= self.limit_seconds
        )


def _loop_stall_order_advice(loop_stall: _LoopStallBound) -> str:
    """What restores ``longest heartbeat timeout < bound <= limit``."""
    from application_sdk.app.task import (  # noqa: PLC0415 — cold path: worker startup only
        _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        HEARTBEAT_TIMEOUT_ENV,
    )
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: worker startup only
        SHUTDOWN_GRACE_SECONDS_ENV,
    )

    if loop_stall.overridden:
        return f"Unset {LOOP_STALL_TIMEOUT_ENV} to derive it"
    consequence = (
        "; until then a stall during the drain can end work Temporal still counts "
        "as alive"
    )
    if loop_stall.seconds <= _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS:
        # Even the default heartbeat timeout does not fit, so no task change helps.
        return (
            f"Raise {SHUTDOWN_GRACE_SECONDS_ENV}: this grace leaves less than the "
            f"default heartbeat timeout ({_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS}s, "
            f"{HEARTBEAT_TIMEOUT_ENV}), so lowering {loop_stall.heartbeat_task} "
            f"alone does not restore the order{consequence}"
        )
    return (
        f"Raise {SHUTDOWN_GRACE_SECONDS_ENV} or lower the heartbeat timeout of "
        f"{loop_stall.heartbeat_task} below {loop_stall.seconds:g}s{consequence}"
    )


def _registered_tasks() -> list[Any]:
    return [
        task
        for tasks in TaskRegistry.get_instance().get_all_tasks().values()
        for task in tasks
    ]


def _longest_heartbeat_timeout() -> tuple[float, str | None]:
    """The longest heartbeat timeout among the registered tasks, and the task
    that sets it when that is longer than the SDK's default (else ``None``).

    A task that holds the event loop for less than its heartbeat timeout is
    still alive to Temporal, so a stall during the drain must outlast it. The
    SDK's default heartbeat timeout is the floor. A task without a heartbeat
    timeout does not count: once the loop stops, it is indistinguishable from
    a freeze, and Temporal reclaims it only at its start-to-close timeout,
    which would leave the bound no use (see ``_unwatched_tasks``).
    """
    from application_sdk.app.task import (  # noqa: PLC0415 — cold path: worker startup only
        _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    )

    longest: tuple[float, str | None] = (
        float(_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS),
        None,
    )
    for task in _registered_tasks():
        if (
            task.heartbeat_timeout_seconds
            and task.heartbeat_timeout_seconds > longest[0]
        ):
            longest = (
                float(task.heartbeat_timeout_seconds),
                f"{task.app_name}.{task.name}",
            )
    return longest


def _unwatched_tasks(bound_seconds: float) -> tuple[str, ...]:
    """App tasks without a heartbeat timeout that may run past the bound.

    The watchdog will not wait for them at shutdown if they hold the loop, so
    their owners are told at startup. The SDK's own tasks are left out: they
    await their I/O rather than hold the loop.
    """
    return tuple(
        f"{task.app_name}.{task.name} ({task.timeout_seconds}s)"
        for task in _registered_tasks()
        if not task.heartbeat_timeout_seconds
        and task.timeout_seconds > bound_seconds
        and not getattr(task.func, "__module__", "").startswith("application_sdk.")
    )


def _loop_stall_bound() -> _LoopStallBound:
    """Derive the stall bound from the registered tasks and the shutdown budget.

    The bound sits just past the longest registered heartbeat timeout, so a
    stall during the drain never ends work Temporal still counts as alive, and
    within the budget, so the exit lands before SIGKILL with daprd's headroom
    left; the budget wins when the two cannot both hold. An explicit
    ``ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS`` replaces it, and ``0`` turns
    the watchdog off.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: worker startup only
        load_shutdown_budget,
        load_worker_loop_stall_timeout_override,
    )

    budget = load_shutdown_budget()
    heartbeat, heartbeat_task = _longest_heartbeat_timeout()
    limit = (
        largest_bound_within(budget.loop_stall_limit_seconds)
        if budget is not None
        else None
    )
    override = load_worker_loop_stall_timeout_override()
    if override is not None:
        seconds, overridden = override, True
    else:
        seconds, overridden = heartbeat + _LOOP_STALL_MARGIN_SECONDS, False
        if limit is not None:
            seconds = min(seconds, limit)
    return _LoopStallBound(
        seconds,
        heartbeat,
        limit,
        overridden=overridden,
        unwatched_tasks=_unwatched_tasks(seconds) if seconds > 0 else (),
        heartbeat_task=heartbeat_task,
        exit_by_seconds=budget.loop_stall_limit_seconds if budget is not None else None,
    )


def _log_exit_deadline_reached(seconds: float) -> None:
    """Label the stack dump the shutdown exit deadline is about to write.

    Called from the running loop, so the dump that may follow is not a stall.
    """
    logger.warning(
        "Shutdown drain still running at the exit deadline: unless it finishes "
        "within %gs, the process dumps every thread's stack and exits with code 1 "
        "before SIGKILL. The event loop is still running, so that dump marks the "
        "deadline, not a stalled loop",
        seconds,
    )


def _install_loop_stall_watchdog(watchdog: LoopStallWatchdog) -> None:
    """Arm the watchdog at the first SIGTERM or SIGINT. Call after the loop's
    own signal handlers are installed, so it chains in front of them."""
    watchdog.install((signal.SIGINT, signal.SIGTERM))
    logger.info(
        "Event-loop stall watchdog set for shutdown: after SIGTERM or SIGINT, if "
        "the event loop does not run for %gs the process dumps every thread's "
        "stack to stderr and exits with code 1",
        watchdog.timeout_seconds,
    )
