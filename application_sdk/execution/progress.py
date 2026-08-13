"""App-facing path to the progress seam (ADR-0018).

The implementation lives in :mod:`application_sdk._runtime.progress`, the SDK's
dependency-neutral substrate, because ``storage/`` has to reach it at module
scope and cannot import from this package (ADR-0019). This module is the
documented import path for app code and re-exports every name unchanged — same
objects, same behaviour::

    from application_sdk.execution.progress import holding_progress

    async with holding_progress("vendor.export", timeout=1800):
        await self.run_in_thread(client.export_everything)

SDK-internal code imports ``application_sdk._runtime.progress`` directly, so
that no SDK module depends on this package for the seam.
"""

from application_sdk._runtime.progress import (
    DEFAULT_MAX_NO_PROGRESS_SECONDS,
    ClosedHold,
    ProgressTracker,
    ProgressWatchdogMode,
    StallObservation,
    bind_progress_tracker,
    current_progress_tracker,
    declared_hold_active,
    holding_progress,
)

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
