"""Dispatch a task on a chosen task queue, faithfully to its ``@task`` metadata.

Only the dispatching workflow can set a Temporal activity's task queue, so
routing has to happen here rather than inside the activity.

:func:`run_on_queue` deliberately does **not** build its own activity command.
It calls the task's ordinary bound method and passes the queue through, so a
routed dispatch and an unrouted one are the same code path in
``_create_task_activity_wrapper``: the same activity name, the same
``[TaskContext, input]`` argument pair, the same eviction-retry loop, the same
retry-policy conversion, and every execution parameter the decorator declared —
``schedule_to_close``, the progress watchdog, the run-age the SLA alert reads.

That is the whole reason this lives in the SDK. The first app to ship size-tier
routing had to restate the dispatch, and the copy drifted within weeks: it
dropped the retry policy's initial interval, never learned
``schedule_to_close_timeout``, and built a ``TaskContext`` without
``run_started_at_epoch``, ``progress_watchdog`` or ``max_no_progress_seconds``.
Each of those silently applies to unrouted activities and not to routed ones,
which is the worst shape a divergence can take — the routed path is the one
carrying the work that was already struggling. One dispatcher, one set of
semantics.

The SDK's static pool routing (``@task(pool="heavy")``, resolved by
``resolve_pool_queue``) stays what it is: a queue fixed at decoration time. A
size tier is a property of the run, not of the task, so it is supplied per
dispatch and takes precedence over the decorator's pool for that call.
"""

from __future__ import annotations

from typing import Any


async def run_on_queue(
    app: Any, task_name: str, arg: Any, task_queue: str | None
) -> Any:
    """Run ``app.<task_name>(arg)``, on ``task_queue`` when one is given.

    ``task_queue=None`` takes the ordinary bound-method path, byte-identical to
    calling the task directly — the same activity name, the same options, the
    same command in workflow history. That is what makes an unrouted run
    indistinguishable from one on a build without tiering, and it is the path
    every run takes while ``ATLAN_APP_SIZE_QUEUE_MAP`` is unset.

    Args:
        app: The app instance, inside a workflow, with its tasks bound.
        task_name: The bare task name, as ``@task`` registered it.
        arg: The task's single typed input.
        task_queue: Queue to dispatch on, or ``None`` to leave it where it is —
            normally ``queues.resolve(ladder.tier_for(...), queue_map)``.

    Returns:
        Whatever the task returns.
    """
    task = getattr(app, task_name)
    if task_queue is None:
        return await task(arg)
    return await task(arg, task_queue=task_queue)
