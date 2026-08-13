"""Warn-mode telemetry for the stall watchdog (ADR-0018).

The obvious question from any app author is *"how do I find every place in my
codebase that needs a hold, and what allowance do I put on each one?"*
Answering it with "audit your code" would reintroduce ADR-0018's own problem one
level down: a per-site number, guessed up front, by hand. The watchdog already
observes exactly what that audit would look for, so it reports instead, and the
whole-codebase audit collapses into reading a report.

Two shapes are emitted, and they are the two things that need action:

1. **No-progress gaps with no hold in force** —
   :func:`record_no_progress_gap`. The sites that need a progress hook or a
   hold.
2. **Long holds, and long *unbounded* holds above all** —
   :func:`record_closed_hold`, fed by every
   :meth:`~application_sdk.execution.progress.ProgressTracker.exit_hold`.
   The sites that want an explicit allowance rather than the duration backstop.
   Without this shape a blocking call offloaded through ``run_in_thread`` is
   invisible to the audit *precisely because it is auto-vouched-for*.

Both shapes are a metric plus an **INFO** log — never WARNING. Warn mode is a
fleet-wide default, so a stall observation under it is an expected observation
rather than an actionable failure, and emitting it at WARNING would manufacture
exactly the alert noise ADR-0018 exists to reduce. Dashboards and the per-app
work-list read the metric; the log is the same finding for whoever happens to be
reading one app's logs. The only WARNING here is a telemetry failure, which is a
statement about the report being incomplete, not about the app.

Ranking per app needs no attribute on these instruments: ``app.name`` is inlined
onto every series from the OTel Resource (see
``observability/_prometheus_enrichment.py``), and the rest of the app identity
(version, release channel, k8s topology) is reachable through ``target_info``.
So these instruments carry only what distinguishes *sites within* an app, which
is what keeps them rankable per app and aggregatable fleet-wide at once.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from application_sdk._runtime.progress import (
    DEFAULT_MAX_NO_PROGRESS_SECONDS,
    ClosedHold,
)
from application_sdk.execution.progress import ProgressWatchdogMode
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Meter for the signals this module owns. Deliberately not the Temporal meter:
#: neither the tracker nor the watchdog has a Temporal dependency, so both also
#: report from local runs.
_METER_NAME = "application_sdk.execution"

#: Lazily created singletons — one instrument per process, built on first use so
#: nothing is bound before ``run_main()`` configures the ``MeterProvider``.
_INSTRUMENTS: dict[str, Any] = {}


def _no_progress_gap_histogram() -> Any:
    """Gap-duration histogram, created on first stall observation.

    A histogram rather than a counter because the fleet-wide *distribution* of
    no-progress gaps is what sizes ``max_no_progress_seconds`` before anything
    enforces (ADR-0018 → *Decisions taken*).
    """
    if "no_progress_gap" not in _INSTRUMENTS:
        from opentelemetry import (  # noqa: PLC0415 — cold path: only reached on a stall observation
            metrics as _otel_metrics,
        )

        _INSTRUMENTS["no_progress_gap"] = _otel_metrics.get_meter(
            _METER_NAME
        ).create_histogram(
            "task.no_progress_gap",
            unit="s",
            description=(
                "Seconds a task attempt went without an observable progress "
                "signal, recorded once per gap that exceeded "
                "max_no_progress_seconds. Partitioned by task, the last "
                "progress label seen, and the watchdog mode — so warn-mode "
                "observations and enforced kills aggregate separately."
            ),
        )
    return _INSTRUMENTS["no_progress_gap"]


def _hold_duration_histogram() -> Any:
    """Hold-duration histogram, created on the first hold this process closes.

    Every closed hold is recorded, not only the long ones: choosing an allowance
    for a site means reading that site's observed distribution (ADR-0018 asks
    for its p99 plus headroom), and a metric that only fired above a threshold
    could not answer that question.
    """
    if "hold_duration" not in _INSTRUMENTS:
        from opentelemetry import (  # noqa: PLC0415 — cold path: only reached once a hold closes
            metrics as _otel_metrics,
        )

        _INSTRUMENTS["hold_duration"] = _otel_metrics.get_meter(
            _METER_NAME
        ).create_histogram(
            "task.hold_duration",
            unit="s",
            description=(
                "Seconds an operation held the stall watchdog, recorded once "
                "per hold released. Partitioned by task, the hold's label, "
                "whether an allowance was declared, and whether the operation "
                "outlived it — so the long *unbounded* holds (the sites that "
                "want an explicit allowance) and the lapsed ones (the "
                "allowances that are too tight) rank separately, and each "
                "site's own distribution is what sizes its allowance."
            ),
        )
    return _INSTRUMENTS["hold_duration"]


def record_no_progress_gap(
    task_name: str, stalled_for: float, last_label: str, mode: ProgressWatchdogMode
) -> None:
    """Record one no-progress gap. Never raises.

    The metric is emitted in *both* warn and enforce mode: in warn mode it is
    the whole point (nothing else reports the gap), and in enforce mode it is
    what makes the kill rate measurable.
    """
    try:
        _no_progress_gap_histogram().record(
            stalled_for,
            {
                "task.name": task_name,
                # A progress label identifies a call site, never a value — see
                # ProgressTracker.enter_hold — so this stays bounded.
                "progress.last_label": last_label,
                "watchdog.mode": mode.value,
            },
        )
    # Best-effort observability: a metric-backend problem must never fail the
    # activity the watchdog is only observing.
    except Exception:
        logger.warning(
            "Failed to record the no-progress gap metric for task '%s' (%.0fs gap); "
            "the stall is still logged but will be missing from the fleet-wide "
            "gap distribution",
            task_name,
            stalled_for,
            exc_info=True,
        )


def record_closed_hold(
    closed: ClosedHold,
    *,
    task_name: str,
    budget_seconds: float = DEFAULT_MAX_NO_PROGRESS_SECONDS,
) -> None:
    """Record one released hold — the second warn-mode shape. Never raises.

    Every hold reaches the metric, so each site's own duration distribution is
    available to size its allowance from. Only a *notable* hold reaches the log,
    because once ``run_in_thread`` auto-holds every blocking offload a line per
    hold would be a line per blocking call — noise that would bury the two
    findings worth acting on:

    - an **unbounded** hold that ran longer than the no-progress budget. It
      would have tripped the watchdog had it not been auto-vouched-for, so it is
      on the work-list: declaring an allowance bounds a wedge there at
      ``allowance + budget`` instead of leaving it to the duration backstop.
    - a hold that **lapsed** — the human's own declared allowance was outlived,
      so the watchdog resumed while the operation was still running. Notable at
      any duration precisely because nobody had to invent the threshold; too
      tight an allowance is what turns a healthy slow call into a false kill
      once anything enforces.

    A long *bounded* hold that stayed inside its allowance is neither: somebody
    already looked at that site and sized it.

    Args:
        closed: The observation handed over by ``exit_hold``.
        task_name: Task the hold was taken in, for the log and the metric.
        budget_seconds: The task's ``max_no_progress_seconds``. Used only as the
            floor for the unbounded-hold log line — the metric records
            everything either way.
    """
    _record_hold_duration(closed, task_name)

    if closed.lapsed:
        logger.info(
            "Task '%s' held the stall watchdog at '%s' for %.0fs, outliving the "
            "%.0fs allowance declared for it; the watchdog resumed while the "
            "operation was still running — raise the allowance if this site is "
            "healthy, since too tight an allowance is what turns a slow call "
            "into a failed attempt once the watchdog enforces",
            task_name,
            closed.label or "<unlabelled>",
            closed.duration_seconds,
            # Never None here: `lapsed` is False for an unbounded hold.
            closed.allowance_seconds,
        )
        return

    if not closed.bounded and closed.duration_seconds >= budget_seconds:
        logger.info(
            "Task '%s' held the stall watchdog at '%s' for %.0fs with no declared "
            "allowance (no-progress budget %.0fs); this site is on the work-list "
            "— wrap it in holding_progress(timeout=...) so a wedge here is caught "
            "at allowance + budget rather than left to the duration backstop",
            task_name,
            closed.label or "<unlabelled>",
            closed.duration_seconds,
            budget_seconds,
        )


def closed_hold_observer(
    task_name: str, *, budget_seconds: float = DEFAULT_MAX_NO_PROGRESS_SECONDS
) -> Callable[[ClosedHold], None]:
    """Build the ``on_hold_closed`` observer for one attempt's tracker.

    Args:
        task_name: Task this tracker belongs to.
        budget_seconds: The task's ``max_no_progress_seconds``; see
            :func:`record_closed_hold`.

    Returns:
        A callable taking one :class:`ClosedHold`, to hand to
        :class:`~application_sdk.execution.progress.ProgressTracker`. It never
        raises, and the tracker guards it a second time — telemetry must never
        fail the activity it is only observing.
    """
    return functools.partial(
        record_closed_hold, task_name=task_name, budget_seconds=budget_seconds
    )


def _record_hold_duration(closed: ClosedHold, task_name: str) -> None:
    try:
        _hold_duration_histogram().record(
            closed.duration_seconds,
            {
                "task.name": task_name,
                # A hold label identifies a call site, never a value — see
                # ProgressTracker.enter_hold — so this stays bounded.
                "hold.label": closed.label,
                # The shape that needs action. Kept as a dimension rather than
                # split into two instruments so one query ranks both.
                "hold.bounded": str(closed.bounded).lower(),
                "hold.lapsed": str(closed.lapsed).lower(),
            },
        )
    # Best-effort observability, exactly as for the gap metric above.
    except Exception:
        logger.warning(
            "Failed to record the hold duration metric for task '%s' (hold '%s', "
            "%.0fs); the per-app hold work-list will be missing this observation",
            task_name,
            closed.label or "<unlabelled>",
            closed.duration_seconds,
            exc_info=True,
        )
