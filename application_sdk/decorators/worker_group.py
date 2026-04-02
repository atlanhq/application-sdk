"""Worker group decorator for routing activities to dedicated Temporal task queues."""

from typing import Any, Callable

WORKER_GROUP_METADATA_KEY = "__worker_group_metadata__"

# Sentinel name for the default worker group.  Activities decorated with
# ``@worker_group(name=DEFAULT_WORKER_GROUP)`` are routed to the base task
# queue (``atlan-{app}-{deployment}``) — the same queue the single-worker
# deployment uses today — preserving backwards compatibility.
DEFAULT_WORKER_GROUP = "default"


def worker_group(name: str) -> Callable[[Any], Any]:
    """Mark an activity as belonging to a named worker group.

    When ``splitDeploymentEnabled`` and ``workerGroups`` are configured in the
    Helm chart, each group gets its own worker deployment listening on a
    dedicated Temporal task queue named
    ``atlan-{app}-{deployment}-{group_name}``.

    Place this decorator **outermost** (above ``@activity.defn``) so the
    metadata is attached to the final callable that the workflow references:

    .. code-block:: python

        @worker_group(name="parse-query")
        @activity.defn
        @auto_heartbeater
        async def parse_query_activity(self, workflow_args):
            ...

    The ``name`` must match the key used in ``workerGroups`` in ``values.yaml``
    so that the computed task queue name is consistent between the decorator and
    the Helm-configured worker deployment.

    Args:
        name: Worker group name (e.g. ``"parse-query"``).  Combined with
            ``APPLICATION_NAME`` and ``DEPLOYMENT_NAME`` to produce the task
            queue ``atlan-{app}-{deployment}-{name}``.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, WORKER_GROUP_METADATA_KEY, {"name": name})
        return func

    return decorator
