"""App-facing path to the progress seam, and the watchdog's own vocabulary (ADR-0018).

The tracker and the hold machinery live in
:mod:`application_sdk._runtime.progress`, the SDK's dependency-neutral substrate,
because ``storage/`` has to reach them at module scope and cannot import from this
package (ADR-0019). This module is the documented import path for app code and
re-exports every one of those names unchanged — same objects, same behaviour::

    from application_sdk.execution.progress import holding_progress

    async with holding_progress("vendor.export", timeout=1800):
        await self.run_in_thread(client.export_everything)

SDK-internal code imports ``application_sdk._runtime.progress`` directly for the
seam, so that no SDK module depends on this package to reach it.

:class:`ProgressWatchdogMode` is defined *here* rather than in the substrate. It is
the watchdog's vocabulary, not the tracker's: nothing in ``_runtime`` reads it, and
its only consumers — :func:`~application_sdk.execution.heartbeat.auto_heartbeat_loop`,
the stall check, and the telemetry that labels a gap with it — are all in this layer.
Keeping it here is also what lets the substrate stay free of any
``application_sdk.contracts`` dependency, since the enum needs
:class:`~application_sdk.contracts.base.SerializableEnum` and ``contracts`` reaches
``storage.ops`` transitively.
"""

import os

from application_sdk._runtime.progress import (
    DEFAULT_MAX_NO_PROGRESS_SECONDS,
    ClosedHold,
    ProgressTracker,
    StallObservation,
    bind_progress_tracker,
    current_progress_tracker,
    declared_hold_active,
    holding_progress,
)
from application_sdk.common._env import env_int
from application_sdk.contracts.base import SerializableEnum
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_MAX_NO_PROGRESS_SECONDS",
    "MAX_NO_PROGRESS_SECONDS",
    "PROGRESS_WATCHDOG_MODE",
    "ClosedHold",
    "ProgressTracker",
    "ProgressWatchdogMode",
    "StallObservation",
    "bind_progress_tracker",
    "current_progress_tracker",
    "declared_hold_active",
    "holding_progress",
    "resolve_max_no_progress_seconds",
    "resolve_watchdog_mode",
]


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
    """No gap is reported and no attempt is ever failed. A kill-switch, not the
    normal state.

    Precisely: the watchdog does not run, so ``task_no_progress_gap_seconds`` and
    its INFO log stop entirely. What does *not* stop is the hold telemetry —
    ``activities.py`` attaches the ``on_hold_closed`` observer to every attempt's
    tracker regardless of mode, so ``task_hold_duration_seconds`` still records
    once per released hold (i.e. once per ``run_in_thread`` offload). Marking
    progress also still happens, at the cost of a clock read.

    That is deliberate — a hold observation is an author's work-list entry, not a
    watchdog action — but it means ``off`` is *not* byte-identical to
    pre-ADR-0018 behaviour, and it is not the lever for reducing wedge exposure:
    the bound on a wedged attempt is ``start_to_close``, which this does not
    touch. See the [stalled-task runbook](../../docs/runbooks/stalled-task.md)."""

    WARN = "warn"
    """Report every gap as a metric and an INFO log; never fail an activity.
    The fleet-wide default, and the audit tool that produces each app's
    work-list."""

    ENFORCE = "enforce"
    """Report the gap, then fail the activity through the injected
    ``on_stall`` handler."""


def _load_watchdog_mode() -> ProgressWatchdogMode:
    """Read the fleet-wide watchdog mode. ``warn`` unless told otherwise.

    ``warn`` is the default because it *cannot fail an activity*, so an opt-in
    would buy nothing and would cost a ~20-team coordination step of exactly the
    kind FND-165 documents stalling (ADR-0018 → *Warn mode is the default,
    fleet-wide*). The env var is therefore mostly the ``off`` kill-switch: a
    deployment that needs even the telemetry gone sets it without a code change
    and without waiting on an SDK release.

    Never raises. An unreadable value falls back to ``warn`` with a complaint
    rather than stopping the worker booting: a typo in one deployment manifest
    must cost one config value, not the process.
    """
    raw = os.environ.get("ATLAN_PROGRESS_WATCHDOG", "").strip().lower()
    if not raw:
        return ProgressWatchdogMode.WARN

    # A lookup rather than `ProgressWatchdogMode(raw)` in a try/except: a typo is
    # not an exceptional condition here, and the traceback from a failed enum
    # lookup would tell the operator reading this line nothing the message does
    # not already say.
    mode = {m.value: m for m in ProgressWatchdogMode}.get(raw)
    if mode is not None:
        return mode

    logger.warning(
        "Ignoring ATLAN_PROGRESS_WATCHDOG=%r: expected one of %s. Falling back "
        "to '%s', so no-progress gaps are still reported and nothing is failed "
        "for them",
        raw,
        ", ".join(repr(m.value) for m in ProgressWatchdogMode),
        ProgressWatchdogMode.WARN.value,
    )
    return ProgressWatchdogMode.WARN


def _load_max_no_progress_seconds() -> float:
    """Read the fleet-wide no-progress allowance, in seconds.

    Defaults to :data:`DEFAULT_MAX_NO_PROGRESS_SECONDS` (900s). ADR-0018 →
    *Decisions taken* records that number as provisional and settled by
    measurement rather than argument, which is exactly why it is an env var: the
    fleet's warn-mode gap distribution sets the real value (FND-297) as a config
    change rather than an SDK release.

    Never raises, and never yields an unusable allowance: zero or negative would
    make every attempt stall on its first tick, which in an enforcing app turns
    one typo into a fleet-wide kill switch.
    """
    raw = env_int("ATLAN_MAX_NO_PROGRESS_SECONDS", 0)
    if raw > 0:
        return float(raw)
    if raw < 0:
        logger.warning(
            "Ignoring ATLAN_MAX_NO_PROGRESS_SECONDS=%d: a no-progress allowance "
            "must be positive. Falling back to %.0fs — set ATLAN_PROGRESS_WATCHDOG "
            "to 'off' to disable the watchdog deliberately",
            raw,
            DEFAULT_MAX_NO_PROGRESS_SECONDS,
        )
    return DEFAULT_MAX_NO_PROGRESS_SECONDS


#: The fleet-wide watchdog mode, read once at import so it is stable for the
#: process lifetime (the same pattern as the ``@task`` timeout defaults and
#: ``RUN_LENGTH_SLA_SECONDS``).
PROGRESS_WATCHDOG_MODE: ProgressWatchdogMode = _load_watchdog_mode()

#: The fleet-wide no-progress allowance in seconds, read once at import.
MAX_NO_PROGRESS_SECONDS: float = _load_max_no_progress_seconds()


def resolve_watchdog_mode(
    declared: ProgressWatchdogMode | None,
) -> ProgressWatchdogMode:
    """Resolve one task's effective mode against the process-wide setting.

    Two rules, and the second is the whole reason this is a function rather than
    an ``or``:

    1. A task that declares nothing inherits :data:`PROGRESS_WATCHDOG_MODE`.
    2. **``off`` in the environment wins over any declaration.** It is the
       kill-switch ADR-0018 → *Rollout* step 3 ships alongside warn-by-default,
       and a switch a per-task ``enforce`` could out-vote would be no use to the
       operator holding it during an incident — which is the only time it gets
       thrown.

    Resolved on the activity side on purpose: the watchdog runs there, so the
    environment that has to be consulted is the one the watchdog is running in,
    not the workflow's.

    Args:
        declared: The task's own ``progress_watchdog``, or ``None`` when it
            declares nothing.

    Returns:
        The mode the watchdog should run in for this attempt.
    """
    if PROGRESS_WATCHDOG_MODE is ProgressWatchdogMode.OFF:
        return ProgressWatchdogMode.OFF
    return declared or PROGRESS_WATCHDOG_MODE


def resolve_max_no_progress_seconds(declared: float | None) -> float:
    """Resolve one task's effective allowance against the process-wide setting.

    No kill-switch rule here, unlike :func:`resolve_watchdog_mode`: turning the
    watchdog off is what ``off`` is for, and an env var that could silently
    shrink a task's declared allowance would be a fleet-wide false-kill
    generator wearing a config knob's clothes.

    Args:
        declared: The task's own ``max_no_progress_seconds``, or ``None``.

    Returns:
        The allowance in seconds.
    """
    return MAX_NO_PROGRESS_SECONDS if declared is None else declared
