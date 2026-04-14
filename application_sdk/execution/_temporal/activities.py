"""Temporal activity definitions for App tasks.

Each @task method on an App becomes a named Temporal activity via
create_activity_from_task(). The activity:
- Receives a strongly-typed Input (the task's input_type)
- Returns a strongly-typed Output (the task's output_type)
- Supports heartbeating for long-running operations
- Converts NonRetryableError to Temporal's ApplicationError
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from temporalio import activity

from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import TaskMetadata
from application_sdk.constants import TRACKED_FILE_REFS_KEY
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclasses.dataclass
class TaskContext:
    """Context passed to task execution.

    Contains metadata needed to set up the app instance.
    """

    app_name: str
    """Name of the parent app."""

    task_name: str
    """Name of the task being executed."""

    run_id: str
    """Workflow run ID."""

    heartbeat_timeout_seconds: int | None = 60
    """Heartbeat timeout in seconds. Set to None to disable heartbeating."""

    auto_heartbeat_seconds: int | None = 20
    """Auto-heartbeat interval in seconds. Set to None for manual heartbeats only."""


def _track_file_refs(workflow_id: str, *refs: FileReference) -> None:
    """Add FileReference objects to the per-workflow tracking set in _app_state.

    Thread-safe: acquires _app_state_lock for the full read-modify-write so
    concurrent activities cannot clobber each other's additions.
    """
    if not refs:
        return
    from application_sdk.app.base import _app_state, _app_state_lock

    with _app_state_lock:
        state = _app_state.setdefault(workflow_id, {})
        tracked: set[FileReference] = state.setdefault(TRACKED_FILE_REFS_KEY, set())
        tracked.update(refs)


def create_activity_from_task(
    task_metadata: TaskMetadata,
) -> Callable[..., Any]:
    """Create a Temporal activity function from a task.

    Args:
        task_metadata: Metadata about the task (input/output types, timeouts, etc.).

    Returns:
        A decorated Temporal activity function.
    """
    activity_name = f"{task_metadata.app_name}:{task_metadata.name}"
    input_type = task_metadata.input_type
    output_type = task_metadata.output_type

    async def activity_fn(context: TaskContext, input_data: Input) -> Output:
        """Execute the task as a Temporal activity."""
        from application_sdk.app.context import AppContext, TaskExecutionContext
        from application_sdk.execution.heartbeat import (
            NoopHeartbeatController,
            TemporalHeartbeatController,
            auto_heartbeat_loop,
        )

        app_registry = AppRegistry.get_instance()
        app_metadata = app_registry.get(context.app_name)
        app_instance = app_metadata.app_cls()

        run_id = context.run_id or str(uuid4())

        # Read correlation_id from ContextVar (set by CorrelationContextInterceptor)
        from application_sdk.observability.correlation import get_correlation_context

        corr_ctx = get_correlation_context()
        correlation_id = corr_ctx.correlation_id if corr_ctx else ""

        app_context = AppContext(
            app_name=context.app_name,
            app_version=app_metadata.version,
            run_id=run_id,
            correlation_id=correlation_id,
        )

        from application_sdk.infrastructure.context import get_infrastructure

        infra = get_infrastructure()
        if infra is not None:
            app_context._state_store = infra.state_store
            app_context._secret_store = infra.secret_store
            app_context._storage = infra.storage

        app_instance._context = app_context

        # Create heartbeat controller based on configuration
        if context.heartbeat_timeout_seconds is not None:
            heartbeat_controller: (
                TemporalHeartbeatController | NoopHeartbeatController
            ) = TemporalHeartbeatController()
        else:
            heartbeat_controller = NoopHeartbeatController()

        task_exec_context = TaskExecutionContext(
            app_context=app_context,
            task_name=context.task_name,
            heartbeat_controller=heartbeat_controller,
        )
        app_instance._task_context = task_exec_context

        stop_event = asyncio.Event()
        heartbeat_task = None

        if (
            context.heartbeat_timeout_seconds is not None
            and context.auto_heartbeat_seconds is not None
        ):
            heartbeat_task = asyncio.create_task(
                auto_heartbeat_loop(
                    interval_seconds=context.auto_heartbeat_seconds,
                    heartbeat_fn=heartbeat_controller.heartbeat_keepalive,
                    stop_event=stop_event,
                    task_name=context.task_name,
                )
            )

        try:
            from application_sdk.storage.file_ref_sync import (
                has_refs_to_materialize,
                has_refs_to_persist,
                materialize_file_refs,
                persist_file_refs,
            )

            method_name = getattr(task_metadata.func, "__name__", task_metadata.name)
            task_method = getattr(app_instance, method_name)

            # Resolve the store once for both FileReference hooks.
            store = infra.storage if infra is not None else None

            # Materialise any durable FileReferences in the input before the task runs.
            if store is not None and has_refs_to_materialize(input_data):
                input_data = await materialize_file_refs(store, input_data)

            result = await task_method(input_data)

            # Persist any ephemeral FileReferences in the output after the task completes.
            if store is not None and has_refs_to_persist(result):
                from application_sdk.execution._temporal.activity_utils import (
                    build_output_path,
                )

                try:
                    output_path: str | None = build_output_path()
                except Exception:
                    logger.warning(
                        "build_output_path() failed, proceeding without output path",
                        exc_info=True,
                    )
                    output_path = None
                result = await persist_file_refs(store, result, output_path=output_path)

            # Track all FileReference local paths for on_complete() cleanup.
            from application_sdk.storage.file_ref_sync import _find_file_refs

            all_refs = _find_file_refs(input_data) + _find_file_refs(result)
            if all_refs:
                _track_file_refs(activity.info().workflow_id, *all_refs)

            return cast("Output", result)

        except Exception as e:
            from application_sdk.app.base import NonRetryableError

            if isinstance(e, NonRetryableError):
                from application_sdk.execution.errors import ApplicationError

                raise ApplicationError(
                    str(e),
                    type=type(e).__name__,
                    non_retryable=True,
                ) from e
            raise

        finally:
            if heartbeat_task is not None:
                stop_event.set()
                try:
                    await asyncio.wait_for(heartbeat_task, timeout=1.0)
                except (TimeoutError, Exception, BaseException):
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except (Exception, BaseException):
                        logger.debug(
                            "Heartbeat task did not cancel cleanly", exc_info=True
                        )

            app_instance._task_context = None
            app_instance._context = None

    # Set type annotations with the actual input/output types from task metadata.
    # This is critical for Temporal to properly deserialize the input dataclass.
    activity_fn.__annotations__ = {
        "context": TaskContext,
        "input_data": input_type,
        "return": output_type,
    }

    decorated = activity.defn(name=activity_name)(activity_fn)
    decorated._task_metadata = task_metadata  # type: ignore[attr-defined]
    decorated._activity_name = activity_name  # type: ignore[attr-defined]

    return decorated


def get_all_task_activities() -> list[Callable[..., Any]]:
    """Get all registered tasks as Temporal activity functions."""
    activities: list[Callable[..., Any]] = []
    task_registry = TaskRegistry.get_instance()

    for task_list in task_registry.get_all_tasks().values():
        for task_meta in task_list:
            activity_fn = create_activity_from_task(task_meta)
            activities.append(activity_fn)

    return activities


def get_activity_options(task_metadata: TaskMetadata) -> dict[str, Any]:
    """Get Temporal activity options from task metadata.

    Args:
        task_metadata: The task metadata.

    Returns:
        Dict of activity options for workflow.execute_activity().
    """
    from temporalio.common import RetryPolicy as TemporalRetryPolicy

    if task_metadata.retry_policy is not None:
        rp = task_metadata.retry_policy
        retry_policy = TemporalRetryPolicy(
            maximum_attempts=rp.max_attempts,
            initial_interval=rp.initial_interval,
            maximum_interval=rp.max_interval,
            backoff_coefficient=rp.backoff_coefficient,
            non_retryable_error_types=list(rp.non_retryable_errors),
        )
    else:
        retry_policy = TemporalRetryPolicy(
            maximum_attempts=task_metadata.retry_max_attempts,
            maximum_interval=timedelta(
                seconds=task_metadata.retry_max_interval_seconds
            ),
        )

    return {
        "start_to_close_timeout": timedelta(seconds=task_metadata.timeout_seconds),
        "retry_policy": retry_policy,
    }
