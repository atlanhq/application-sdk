"""Workflow utility functions for activity dispatch.

Use :func:`execute_activity` and :func:`execute_activity_method` in place of
``workflow.execute_activity`` / ``workflow.execute_activity_method`` when
activities are decorated with ``@worker_group``.  The wrappers automatically
resolve the correct Temporal task queue from the decorator metadata so
workflow code never hard-codes queue names.

Example::

    from application_sdk.workflows.utils import execute_activity_method

    class MyWorkflow(WorkflowInterface):
        @workflow.run
        async def run(self, config):
            result = await execute_activity_method(
                self.activities_cls.parse_query_activity,
                args=[config],
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=5),
            )
"""

from typing import Any, Callable, Optional

from temporalio import workflow

from application_sdk.constants import APPLICATION_NAME, DEPLOYMENT_NAME
from application_sdk.decorators.worker_group import (
    DEFAULT_WORKER_GROUP,
    WORKER_GROUP_METADATA_KEY,
)


def _get_task_queue_for_activity(func: Callable[..., Any]) -> Optional[str]:
    """Return the Temporal task queue for *func* based on its ``@worker_group`` metadata.

    Walks the ``__wrapped__`` chain produced by ``functools.wraps`` (e.g.
    ``@auto_heartbeater``) and unwraps bound methods via ``__func__`` so the
    lookup works for both sync and async activities, regardless of decorator order.

    Queue name rules:

    * No ``@worker_group`` → returns ``None`` (caller uses the worker's own queue unchanged).
    * ``@worker_group(name="default")`` → returns the base queue
      (``atlan-{app}-{deployment}``), preserving backwards compatibility.
    * ``@worker_group(name="<group>")`` → returns ``atlan-{app}-{deployment}-{group}``.
    """
    candidate: Optional[Callable[..., Any]] = getattr(func, "__func__", func)
    while candidate is not None:
        metadata = getattr(candidate, WORKER_GROUP_METADATA_KEY, None)
        if metadata:
            group_name: str = metadata["name"]
            if group_name == DEFAULT_WORKER_GROUP:
                # "default" maps to the existing base queue — no suffix appended.
                if DEPLOYMENT_NAME:
                    return f"atlan-{APPLICATION_NAME}-{DEPLOYMENT_NAME}"
                return APPLICATION_NAME
            if DEPLOYMENT_NAME:
                return f"atlan-{APPLICATION_NAME}-{DEPLOYMENT_NAME}-{group_name}"
            return f"atlan-{APPLICATION_NAME}-{group_name}"
        candidate = getattr(candidate, "__wrapped__", None)
    return None


def execute_activity(
    activity_func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Drop-in replacement for ``workflow.execute_activity`` with automatic task queue routing.

    If *activity_func* is annotated with ``@worker_group``, the task queue is
    resolved from the metadata and injected into *kwargs* unless the caller has
    already supplied one.

    All positional and keyword arguments are forwarded verbatim to
    ``workflow.execute_activity``.
    """
    if not kwargs.get("task_queue"):
        task_queue = _get_task_queue_for_activity(activity_func)
        if task_queue:
            kwargs["task_queue"] = task_queue
    return workflow.execute_activity(activity_func, *args, **kwargs)


def execute_activity_method(
    activity_method: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Drop-in replacement for ``workflow.execute_activity_method`` with automatic task queue routing.

    If *activity_method* is annotated with ``@worker_group``, the task queue is
    resolved from the metadata and injected into *kwargs* unless the caller has
    already supplied one.

    All positional and keyword arguments are forwarded verbatim to
    ``workflow.execute_activity_method``.
    """
    if not kwargs.get("task_queue"):
        task_queue = _get_task_queue_for_activity(activity_method)
        if task_queue:
            kwargs["task_queue"] = task_queue
    return workflow.execute_activity_method(activity_method, *args, **kwargs)
