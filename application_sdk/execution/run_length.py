"""Run-length SLA observation — the duration *alert* (ADR-0018).

Removing the duration knob removes the only wall-clock bound the system has, and
ADR-0018 → *Bounding total time* names what replaces it. Two things are needed,
and this module is the second:

1. A ceiling on the retry product, available as a config change rather than a
   redesign — ``@task(schedule_to_close_seconds=...)`` and
   :func:`~application_sdk.execution.retry.resolve_activity_time_bounds`.
2. **A duration signal for the runs no ceiling catches.** Once the AE layer
   drops its timeouts and ``start_to_close`` is a 24h backstop, a run that
   wedges *while dribbling small amounts of progress* is effectively unbounded:
   every progress mark re-arms the stall watchdog, so it never fires, and the
   run is not stalled by any definition the watchdog has. The replacement for a
   duration kill is a duration alert, so "remove the timeouts" does not quietly
   become "nobody notices for a week".

**Why an alert and not a kill, restated where it is implemented.** The SDK
cannot know how long a healthy run of an app takes against a tenant it has never
seen — that is the number ADR-0018 exists to stop guessing. An *alert* threshold
is a different kind of number from a *kill* threshold: being wrong costs a human
one look, not a run's worth of work. That asymmetry is what makes a default
defensible here where a default duration ceiling is not.

**Where the observation comes from, and what it therefore misses.** The run's
age is measured inside the activity attempt, on the tick the heartbeat loop
already runs (:func:`~application_sdk.execution.heartbeat.auto_heartbeat_loop`),
from the run start time the workflow puts on the activity's ``TaskContext``.
That places it deliberately *outside* workflow code: a workflow-side timer would
be a durable command in every run's history and a determinism hazard across an
SDK upgrade, and the run shapes that matter here — a long extraction dribbling
progress — are inside activities by construction.

The cost of that choice, stated: a run is only observed while at least one of
its activities is executing. A run idle between activities — parked on a signal,
or waiting on a child workflow whose own activities report under *their* run's
identity — is not observed, and neither is a task with heartbeating disabled
(no tick to ride). If a wedge in one of those shapes ever shows up, a
workflow-side timer is the escalation; nothing here forecloses it.

The signal is a **histogram plus one WARNING log per attempt**. WARNING, unlike
the warn-mode stall telemetry next door in ``progress_telemetry`` (INFO, because
a warn-mode stall observation is an expected observation): a run past the length
its own operator declared is not expected, and it is the one thing in ADR-0018's
new world that nothing else reports.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from application_sdk.common._env import env_int
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Meter for the signal this module owns. Shared with ``progress_telemetry``:
#: both are execution-layer observations of one attempt, and neither has a
#: Temporal dependency, so both also report from local runs.
_METER_NAME = "application_sdk.execution"

#: Lazily created singleton instrument, built on first use so nothing is bound
#: before ``run_main()`` configures the ``MeterProvider``.
_INSTRUMENTS: dict[str, Any] = {}


#: 24 hours, as a named constant so the fallback and the documented default
#: cannot drift apart.
_DEFAULT_SLA_SECONDS = 86_400


def _load_sla_seconds() -> float:
    """Read the fleet-wide run-length SLA. ``0`` disables the observation.

    24 hours by default: the same order as the ``start_to_close`` backstop
    ADR-0018 raises the per-attempt ceiling to, so a run that has spent longer
    than one full backstop end-to-end is the thing a human hears about. It is
    the *alert* threshold, so an app whose healthy runs legitimately outlast it
    sets its own value — that declaration is the point, and it costs one env var
    rather than a code change.

    Never raises: a malformed value falls back to the default (``env_int``
    warns), and a negative one disables the observation with a complaint rather
    than alerting on every run from its first tick.
    """
    raw = env_int("ATLAN_RUN_LENGTH_SLA_SECONDS", _DEFAULT_SLA_SECONDS)
    if raw < 0:
        logger.warning(
            "Ignoring ATLAN_RUN_LENGTH_SLA_SECONDS=%d: a run-length SLA must be "
            "positive, or 0 to disable the alert. Disabling it — no run-length "
            "alert will be raised for this app",
            raw,
        )
        return 0.0
    return float(raw)


#: How long the SLA gives a run before an alert is raised, in seconds. ``0``
#: disables the observation entirely. Read once at import, so it is stable for
#: the process lifetime.
RUN_LENGTH_SLA_SECONDS: float = _load_sla_seconds()

#: Shortest gap between two observations of the same run, in seconds. The
#: heartbeat loop ticks every ``auto_heartbeat_seconds`` (10s by default), which
#: is the right cadence for a beat and far too fine for this: an over-SLA run
#: only needs to keep re-asserting often enough that the alert stays firing
#: while it runs and resolves once it ends. One minute keeps the alert window in
#: minutes rather than hours without adding a metric point per beat.
_OBSERVE_INTERVAL_SECONDS = 60.0


def _run_length_histogram() -> Any:
    """Over-SLA run-age histogram, created on the first breach in this process.

    A histogram of the *age*, not a counter of breaches: an operator's first
    question about an alert is "how far over is it?", and the distribution over
    a fleet is also what tells whoever owns the SLA whether the threshold is the
    wrong number rather than the runs being wrong. ``_count`` is what an alert
    rule reads.
    """
    if "run_length_over_sla" not in _INSTRUMENTS:
        from opentelemetry import (  # noqa: PLC0415 — cold path: only reached once a run is over its SLA
            metrics as _otel_metrics,
        )

        _INSTRUMENTS["run_length_over_sla"] = _otel_metrics.get_meter(
            _METER_NAME
        ).create_histogram(
            "task.run_length_over_sla",
            unit="s",
            description=(
                "Age of a run observed to be past its run-length SLA, recorded "
                "once a minute per executing activity attempt for as long as it "
                "stays over. Partitioned by the workflow type and the task that "
                "happened to be running, so an alert on the count names both. "
                "Never recorded for a run inside its SLA — the count going up "
                "at all is the alert."
            ),
        )
    return _INSTRUMENTS["run_length_over_sla"]


@dataclass
class RunLengthWatch:
    """One activity attempt's view of how long its *run* has been going.

    Cheap and stateful: two floats and a bool, ticked by the heartbeat loop.
    Owned by the activity layer (one per attempt), which is also the only place
    that can supply the run start time.

    Attributes:
        run_started_at_epoch: Wall-clock seconds since the epoch at which the
            run started, taken from the workflow side. Compared against this
            worker's own clock, which is why it is the run *length* to a
            resolution of NTP skew — irrelevant against an SLA in hours.
        sla_seconds: How long the run may take before it is worth an alert.
        task_name: The task executing when the observation is made. Recorded so
            the alert names where the run was spending its time, not only that
            it was long.
        workflow_type: The run's workflow type, for the metric. Bounded by the
            number of registered workflows.
        clock: Monotonic source for the re-assert throttle, injected for the
            same reason ``ProgressTracker`` injects one — an asyncio loop shares
            ``time.monotonic``, so patching it globally in tests makes the loop
            itself misbehave.
        wall_clock: Wall-clock source for the run age, injected so a test can
            place a run at any age without waiting for one.
    """

    run_started_at_epoch: float
    sla_seconds: float
    task_name: str
    workflow_type: str = ""
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    _last_observed_at: float | None = field(default=None, init=False, repr=False)
    _reported: bool = field(default=False, init=False, repr=False)

    def observe(self) -> None:
        """Record the run's age if it is over the SLA. Never raises.

        Safe to call on every heartbeat tick: it throttles itself to
        :data:`_OBSERVE_INTERVAL_SECONDS`, and does nothing at all while the run
        is inside its SLA.
        """
        try:
            if self.sla_seconds <= 0 or self.run_started_at_epoch <= 0:
                return

            now = self.clock()
            if (
                self._last_observed_at is not None
                and now - self._last_observed_at < _OBSERVE_INTERVAL_SECONDS
            ):
                return

            age = self.wall_clock() - self.run_started_at_epoch
            if age <= self.sla_seconds:
                # Deliberately not stamped: a run inside its SLA is not an
                # observation this module makes, so the first tick past the SLA
                # reports immediately rather than up to a minute late.
                return

            self._last_observed_at = now

            # The log line comes first so a metric-backend problem cannot also
            # cost the human-readable half of the alert.
            if not self._reported:
                self._reported = True
                logger.warning(
                    "Run has been going for %.0fs, past the %.0fs run-length SLA, "
                    "and is still running task '%s' (workflow type '%s'). Nothing "
                    "will terminate it on time: a duration alert is what replaced "
                    "the duration kill (ADR-0018), so decide whether this run is "
                    "healthy-and-slow — in which case raise "
                    "ATLAN_RUN_LENGTH_SLA_SECONDS for this app — or wedged, in "
                    "which case terminate it and look at the task named here",
                    age,
                    self.sla_seconds,
                    self.task_name,
                    self.workflow_type or "<unknown>",
                )

            self._record(age)
        # Best-effort observability: a clock or metric-backend problem must
        # never fail the attempt this is only observing.
        except Exception:
            logger.warning(
                "Failed to observe the run length for task '%s'; an over-long run "
                "will be missing from the run-length SLA alert",
                self.task_name,
                exc_info=True,
            )

    def _record(self, age_seconds: float) -> None:
        _run_length_histogram().record(
            age_seconds,
            {
                "task.name": self.task_name,
                "temporal.workflow.type": self.workflow_type,
            },
        )


def build_run_length_watch(
    run_started_at_epoch: float | None,
    task_name: str,
    workflow_type: str = "",
    sla_seconds: float | None = None,
) -> RunLengthWatch | None:
    """Build one attempt's watch, or ``None`` when there is nothing to watch.

    Args:
        run_started_at_epoch: Run start, from the workflow side. ``None`` or a
            non-positive value means the dispatching workflow predates this
            field (a run in flight across the upgrade) or the task ran outside a
            workflow — either way there is no run to measure, and no watch.
        task_name: Task executing this attempt.
        workflow_type: The run's workflow type, for the metric attribute.
        sla_seconds: Override the process-wide SLA. Defaults to
            :data:`RUN_LENGTH_SLA_SECONDS`; ``0`` disables.

    Returns:
        A :class:`RunLengthWatch`, or ``None`` when the SLA is disabled or the
        run start is unknown.
    """
    sla = RUN_LENGTH_SLA_SECONDS if sla_seconds is None else sla_seconds
    if sla <= 0 or not run_started_at_epoch or run_started_at_epoch <= 0:
        return None
    return RunLengthWatch(
        run_started_at_epoch=run_started_at_epoch,
        sla_seconds=sla,
        task_name=task_name,
        workflow_type=workflow_type,
    )
