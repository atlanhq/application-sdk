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

import inspect
from typing import Any


def _accepts_task_queue(task: Any) -> bool:
    """Whether ``task`` is the per-run wrapper, which takes a ``task_queue``.

    Inside a workflow run ``_wrap_instance_tasks`` replaces every ``@task``
    method with a wrapper that dispatches a Temporal activity and accepts the
    queue to place it on. Outside one — a local run, a direct unit test of a
    workflow method — the attribute is still the raw decorated function, whose
    body executes in process. There is no activity there and so no queue to
    choose, and passing the keyword would raise ``TypeError: got an unexpected
    keyword argument 'task_queue'``: a hard failure in exactly the paths where
    routing was never going to apply.

    Asking the signature rather than looking for ``@task``'s ``_task_metadata``
    marker, for two reasons. Importing ``application_sdk.app.task.is_task``
    would pull the whole app stack (``application_sdk.app`` → ``base`` →
    pydantic) into a package that workflow code imports to ask for a tier. And
    the marker is an absence test, which any object that manufactures
    attributes on demand — a ``Mock``, a proxy — answers wrongly.

    An uninspectable callable is assumed to accept it: the module's rule is that
    a wrong answer costs something visible, so a failed dispatch is preferable
    to silently not routing.
    """
    try:
        parameters = inspect.signature(task).parameters
    except (TypeError, ValueError):
        # An uninspectable callable is an expected, answerable case rather than
        # a hidden error, and taking a logger here would pull the observability
        # stack (~800 modules) into a module workflow code imports.
        # conformance: ignore[E007] intentional: expected case, and a logger here is not affordable
        return True
    if "task_queue" in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


async def run_on_queue(
    app: Any, task_name: str, arg: Any, task_queue: str | None
) -> Any:
    """Run ``app.<task_name>(arg)``, on ``task_queue`` when one is given.

    No queue — ``None`` or empty — takes the ordinary bound-method path, byte-identical to
    calling the task directly — the same activity name, the same options, the
    same command in workflow history. That is what makes an unrouted run
    indistinguishable from one on a build without tiering, and it is the path
    every run takes while ``ATLAN_APP_SIZE_QUEUE_MAP`` is unset.

    Args:
        app: The app instance, inside a workflow, with its tasks bound.
        task_name: The bare task name, as ``@task`` registered it.
        arg: The task's single typed input.
        task_queue: Queue to dispatch on, or ``None``/empty to leave it where it
            is — normally ``queues.resolve(ladder.tier_for(...), queue_map)``.

    Returns:
        Whatever the task returns.

    Note:
        Outside a workflow run the task is not wrapped as an activity, so there
        is no queue to choose and ``task_queue`` is ignored rather than raising.
    """
    task = getattr(app, task_name)
    # Falsy, not just ``None``: ``resolve`` answers ``None`` for an unrouted
    # tier, but an app computing the queue another way can land on ``""``, and
    # the two have to mean the same thing at both layers.
    if not task_queue or not _accepts_task_queue(task):
        return await task(arg)
    return await task(arg, task_queue=task_queue)
