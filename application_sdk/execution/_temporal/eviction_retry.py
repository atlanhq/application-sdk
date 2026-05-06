"""Per-activity eviction-retry loop for SDK-generated workflows.

Wraps a single ``workflow.execute_activity`` call so that when the activity
fails with ``WorkerEvictedError`` — i.e. the worker pod was terminated by
SIGTERM mid-execution (KEDA scale-down, VPA eviction, spot reclaim, node
drain, rolling deploy) — the workflow re-dispatches the activity as a fresh
attempt without burning the application-error retry budget configured on the
task's :class:`~application_sdk.execution.retry.RetryPolicy`.

Bounded by :data:`~application_sdk.constants.WORKER_EVICTION_MAX_RETRIES`;
when the cap is hit, subsequent eviction failures bubble up like any other
activity failure.

The eviction counter is per call site (per activity invocation), not per
workflow run, so an activity that survives one eviction starts fresh on its
next call within the same workflow.

Determinism note: every operation in this loop is deterministic for Temporal
replay — a bounded integer counter incremented in workflow code and a
deterministic string compare on ``ApplicationError.type``. No clocks, no
random, no I/O.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from application_sdk.app.base import WORKER_EVICTED_TYPE
from application_sdk.constants import WORKER_EVICTION_MAX_RETRIES


def _is_worker_evicted(err: ActivityError) -> bool:
    """Return True iff this ActivityError was caused by worker pod eviction.

    Temporal serialises only the *type string* of the original exception
    across the activity/workflow boundary — the Python class itself is lost.
    The activity wrapper raises ``ApplicationError(type=WORKER_EVICTED_TYPE)``,
    which arrives here as ``ActivityError(cause=ApplicationError(type=...))``.

    Implementation note: ``temporalio.exceptions.TemporalError.cause`` is
    currently a ``@property`` that returns ``self.__cause__``. We rely on
    that contract — if a future temporalio release changes the storage
    (e.g. caches into ``_cause``) without updating the property, this
    detection would silently regress. Pinned via the test fixture in
    ``tests/unit/execution/test_eviction.py`` which mirrors the same shape.
    """
    cause = err.cause
    return isinstance(cause, ApplicationError) and cause.type == WORKER_EVICTED_TYPE


async def execute_activity_with_eviction_retry(
    *args: Any,
    max_eviction_retries: int = WORKER_EVICTION_MAX_RETRIES,
    **kwargs: Any,
) -> Any:
    """Execute a Temporal activity with a worker-eviction retry loop.

    Forwards every positional and keyword argument to
    ``workflow.execute_activity`` unchanged, so the call site looks identical
    to a direct invocation aside from the wrapper name.

    Args:
        *args: Forwarded to ``workflow.execute_activity`` — typically the
            activity name (or function), then ``args=[...]``, etc.
        max_eviction_retries: Override the default cap for this call only.
            Defaults to :data:`WORKER_EVICTION_MAX_RETRIES`.
        **kwargs: Forwarded to ``workflow.execute_activity`` — e.g.
            ``start_to_close_timeout``, ``heartbeat_timeout``, ``retry_policy``,
            ``result_type``, ``summary``.

    Returns:
        Whatever the activity returns.

    Raises:
        ActivityError: When the activity fails for any non-eviction reason,
            or when eviction failures exceed ``max_eviction_retries``.
    """
    eviction_attempts = 0
    while True:
        try:
            return await workflow.execute_activity(*args, **kwargs)
        except ActivityError as err:
            if _is_worker_evicted(err) and eviction_attempts < max_eviction_retries:
                eviction_attempts += 1
                # ``workflow.logger`` is a ``logging.LoggerAdapter`` whose
                # underlying ``Logger._log`` only accepts the stdlib reserved
                # kwargs (``exc_info``, ``extra``, ``stack_info``, ``stacklevel``).
                # Flat custom kwargs raise ``TypeError`` at call time, so the
                # eviction-retry path itself would crash on the first eviction
                # if we did not nest under ``extra``. ``AtlanLoggerAdapter``
                # also reads ``extra`` correctly, so this works in both modes.
                workflow.logger.info(
                    "activity re-dispatched after worker eviction",
                    extra={
                        "eviction_attempts": eviction_attempts,
                        "max_eviction_retries": max_eviction_retries,
                    },
                )
                continue
            raise
