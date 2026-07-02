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
from application_sdk.constants import LOCAL_WORKFLOW_ID, TRACKED_FILE_REFS_KEY
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Temporal's failure serializer (_error_to_failure) recurses into __cause__ /
# __context__ chains without a depth bound.  A deep or cyclic chain hits
# Python's recursion limit and the serializer's own crash replaces the real
# error with "Failed building exception result: maximum recursion depth
# exceeded" (BLDX-1512).  We sever the chain at this depth before raising
# ApplicationError so the serializer can always complete safely.
#
# Safety maths: serializer uses 2 call frames per level; baseline async call
# stack is ~30–50 frames; at depth 50 we reach ~150 frames total vs. Python's
# default limit of 1000.  Each stack trace is typically 1–5 KB, so 50 levels ≈
# 50–250 KB — well inside Temporal's default 2 MB payload limit.
_MAX_CHAIN_DEPTH = 50


def _sever_cause_chain(exc: BaseException) -> None:
    """Sever the __cause__/__context__ chain at _MAX_CHAIN_DEPTH.

    Walks the chain iteratively (safe against deep stacks) and cuts the link
    once we hit the depth cap or detect a cycle via object identity.  Mutates
    the exception objects in place — safe because this is called immediately
    before we raise a fresh ApplicationError that will own the chain.

    At the cut point both ``__cause__`` and ``__context__`` are nulled out so
    that neither link re-opens the chain.  Any un-traversed alternate link
    (e.g. a ``__context__`` branch not followed because ``__cause__`` was
    taken) is also discarded — this is a deliberate trade-off to guarantee
    termination; the primary causal path up to the depth cap is preserved.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        depth += 1
        nxt = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
        if nxt is not None and (depth >= _MAX_CHAIN_DEPTH or id(nxt) in seen):
            current.__cause__ = None
            current.__context__ = None
            break
        current = nxt


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

    workflow_id: str = LOCAL_WORKFLOW_ID
    """Temporal workflow ID. Set by the workflow side so both sites read from one transport."""

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
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
        _app_state,
        _app_state_lock,
    )

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
        from application_sdk.app.context import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            AppContext,
            TaskExecutionContext,
        )
        from application_sdk.execution.heartbeat import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            NoopHeartbeatController,
            TemporalHeartbeatController,
            auto_heartbeat_loop,
        )

        app_registry = AppRegistry.get_instance()
        app_metadata = app_registry.get(context.app_name)
        app_instance = app_metadata.app_cls()

        run_id = context.run_id or str(uuid4())

        # Read correlation_id from ContextVar (set by CorrelationContextInterceptor)
        from application_sdk.observability.correlation import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            get_correlation_context,
        )

        corr_ctx = get_correlation_context()
        correlation_id = corr_ctx.correlation_id if corr_ctx else ""

        app_context = AppContext(
            app_name=context.app_name,
            app_version=app_metadata.version,
            run_id=run_id,
            workflow_id=context.workflow_id,
            correlation_id=correlation_id,
        )

        from application_sdk.infrastructure.context import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            get_infrastructure,
        )

        infra = get_infrastructure()
        if infra is not None:
            app_context._state_store = infra.state_store
            app_context._secret_store = infra.secret_store
            app_context._storage = infra.storage
            app_context._upstream_storage = infra.upstream_storage

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

        # Seed pod/node identity into the heartbeat so an abrupt kill (spot
        # reclaim / OOM) can be attributed after the fact via its last
        # heartbeat details. No-op unless infra diagnosis is enabled.
        from application_sdk.execution._temporal.infra_diagnosis import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            seed_activity_identity,
        )

        seed_activity_identity(heartbeat_controller)

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
            from application_sdk.storage.file_ref_sync import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
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
                from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
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
            from application_sdk.storage.file_ref_sync import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
                _find_file_refs,
            )

            all_refs = _find_file_refs(input_data) + _find_file_refs(result)
            if all_refs:
                _track_file_refs(context.workflow_id, *all_refs)

            return cast("Output", result)

        except asyncio.CancelledError as e:
            # In Python 3.8+, ``asyncio.CancelledError`` extends ``BaseException``,
            # so it bypasses the ``except Exception`` block below. We must catch
            # it explicitly to attribute pod-termination cancels to
            # ``ApplicationError(type=WORKER_EVICTED_TYPE)`` and let other cancels propagate as today.
            #
            # NOTE: converting ``CancelledError`` to a regular exception
            # technically violates asyncio's cancellation protocol — the
            # task ends in done-with-exception state instead of cancelled,
            # so callers keying on ``Task.cancelled()`` would observe False.
            # In practice this only fires during graceful worker shutdown,
            # where the only consumer is Temporal's activity wrapper (which
            # records the failure on the wire as ``ApplicationError`` — the
            # exact behaviour we want so the workflow-side eviction loop can
            # re-dispatch). The activity's ``finally`` block still runs for
            # heartbeat cleanup. If we ever surface this swap to a plain
            # asyncio caller, we'd need to relocate it to a Temporal
            # interceptor instead.
            from application_sdk.execution.shutdown import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
                is_worker_shutting_down,
            )

            if is_worker_shutting_down():
                from application_sdk.errors.leaves import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + errors imports observability transitively
                    WORKER_EVICTED_TYPE,
                )
                from application_sdk.execution.errors import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
                    ApplicationError,
                )

                _sever_cause_chain(e)
                raise ApplicationError(
                    "Activity terminated because the worker pod is shutting down",
                    type=WORKER_EVICTED_TYPE,
                    non_retryable=True,
                ) from e
            raise

        # conformance: ignore[E004] exception translator: both branches re-raise; nothing is swallowed
        except Exception as e:
            from application_sdk.errors.base import (  # noqa: PLC0415 — circular
                AppError as _AppError,
            )

            if isinstance(e, _AppError):
                from application_sdk.execution.errors import (  # noqa: PLC0415 — circular
                    ApplicationError,
                )

                # Guard FailureDetails construction: evidence fields may contain
                # non-serialisable values; fall back to details-free ApplicationError
                # rather than letting a secondary error mask the original.
                try:
                    details: tuple[Any, ...] = (e.to_failure_details(),)
                except Exception:
                    logger.warning(
                        "Failed to build FailureDetails for %s; raising without structured details",
                        type(e).__name__,
                        exc_info=True,
                    )
                    details = ()

                # Sever deep / cyclic __cause__ / __context__ chains before
                # Temporal's failure serializer walks them (BLDX-1512).
                _sever_cause_chain(e)

                raise ApplicationError(
                    str(e),
                    *details,
                    type=type(e).__name__,
                    non_retryable=not e.effective_retryable,
                ) from e
            raise

        finally:
            if heartbeat_task is not None:
                stop_event.set()
                try:
                    await asyncio.wait_for(heartbeat_task, timeout=1.0)
                # conformance: ignore[E004] cleanup path cancelling heartbeat task in finally; all exceptions handled by inner cancel+log
                except (TimeoutError, Exception):
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    # conformance: ignore[E004] heartbeat cancel cleanup in finally; debug-logged with exc_info; swallow is intentional
                    except Exception:
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
    from temporalio.common import (  # noqa: PLC0415 — cold path: only used in retry policy reconstruction
        RetryPolicy as TemporalRetryPolicy,
    )

    from application_sdk.execution.retry import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + retry imports errors transitively
        _with_worker_evicted_non_retryable,
    )

    if task_metadata.retry_policy is not None:
        rp = task_metadata.retry_policy
        retry_policy = TemporalRetryPolicy(
            maximum_attempts=rp.max_attempts,
            initial_interval=rp.initial_interval,
            maximum_interval=rp.max_interval,
            backoff_coefficient=rp.backoff_coefficient,
            non_retryable_error_types=_with_worker_evicted_non_retryable(
                list(rp.non_retryable_errors)
            ),
        )
    else:
        retry_policy = TemporalRetryPolicy(
            maximum_attempts=task_metadata.retry_max_attempts,
            maximum_interval=timedelta(
                seconds=task_metadata.retry_max_interval_seconds
            ),
            non_retryable_error_types=_with_worker_evicted_non_retryable([]),
        )

    return {
        "start_to_close_timeout": timedelta(seconds=task_metadata.timeout_seconds),
        "retry_policy": retry_policy,
    }
