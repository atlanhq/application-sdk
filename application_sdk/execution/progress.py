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
from application_sdk.contracts.base import SerializableEnum

__all__ = [
    "DEFAULT_MAX_NO_PROGRESS_SECONDS",
    "ClosedHold",
    "ProgressTracker",
    "ProgressWatchdogMode",
    "StallObservation",
    "bind_progress_tracker",
    "current_progress_tracker",
    "declared_hold_active",
    "holding_progress",
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
    """Inert. Nothing is observed and nothing is reported — byte-identical to
    pre-ADR-0018 behaviour. A kill-switch, not the normal state."""

    WARN = "warn"
    """Report every gap as a metric and an INFO log; never fail an activity.
    The fleet-wide default, and the audit tool that produces each app's
    work-list."""

    ENFORCE = "enforce"
    """Report the gap, then fail the activity through the injected
    ``on_stall`` handler."""
