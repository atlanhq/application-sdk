"""Workflow failure context interceptor for Temporal workflows.

When a child workflow fails, the parent (Automation Engine) only receives a
ChildWorkflowError with the terminal exception. The structured context of WHICH
activity inside the child failed — at what attempt, with what timeout, whether it
was a config failure — is lost unless explicitly propagated.

This interceptor preserves that context by:
  1. Activity side: on every activity failure, stashing structured context
     (activity_type, attempt, timeout config, error type, config_failure flag)
     in process-level storage keyed by workflow_run_id.
  2. Workflow side: when the workflow reaches a terminal failure, popping the
     stashed context and re-raising as ApplicationError with details[0] = context,
     so the parent workflow can access it from the ChildWorkflowError cause chain.

Architecture note — sandboxing:
    Temporal runs workflow code in a sandboxed environment where threading.Lock
    risks RestrictedWorkflowAccessError. The activity side (runs in threads, outside
    the sandbox) uses _failure_contexts_lock for safe writes. The workflow side (runs
    inside the sandbox) uses dict.pop on a unique run_id key, which is safe in CPython
    without a lock — the same pattern used by OutputInterceptor for _collected_outputs.

Architecture note — stash semantics:
    Each activity failure overwrites the stash for that workflow_run_id. For sequential
    workflows (the common case for AE DAG nodes), the stash always holds the last
    failing activity, which is the one that caused the workflow to fail. On workflow
    success, the finally block cleans up any residual stash entry.

Usage (automatic via SDK — no app code changes needed):
    The parent AE reads the context from the ChildWorkflowError cause chain:

        cause = err  # ChildWorkflowError from workflow.execute_child_workflow()
        while cause.__cause__:
            cause = cause.__cause__
            if isinstance(cause, ApplicationError) and cause.details:
                activity_ctx = cause.details[0]
                # activity_ctx = {
                #   "activity_type": "bulk_entity_create",
                #   "attempt": 3,
                #   "task_queue": "atlan-publish-production",
                #   "start_to_close_timeout": "0:30:00",
                #   "python_error_type": "ConnectionError",
                #   "temporal_error_type": "ApplicationError",  # if app set it
                #   "non_retryable": False,
                #   "config_failure": True,           # only if preflight/circuit-breaker
                #   "config_failure_reason": "...",   # only if config_failure=True
                # }
                break
"""

import threading
from typing import Any, Dict, Optional, Type

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Process-level storage: workflow_run_id → activity failure context dict.
# Written by activity interceptor (with lock); read+cleared by workflow interceptor (no lock).
_failure_contexts: Dict[str, Dict[str, Any]] = {}
_failure_contexts_lock = threading.Lock()


class WorkflowFailureContextActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity interceptor that stashes structured failure context on activity failure.

    On activity failure: captures activity_type, attempt, timeout config, error
    classification, and any explicit config_failure tagging from the exception,
    then stashes it in process-level storage keyed by workflow_run_id.

    On activity success: no-op.
    On stash failure: logs warning and never masks the original exception.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        try:
            return await super().execute_activity(input)
        except BaseException as e:
            self._stash_failure_context(e)
            raise

    def _stash_failure_context(self, exception: BaseException) -> None:
        """Build and stash a structured failure context dict for this activity execution."""
        try:
            info = activity.info()

            ctx: Dict[str, Any] = {
                "activity_type": info.activity_type,
                "attempt": info.attempt,
                "task_queue": info.task_queue,
                "python_error_type": type(exception).__name__,
                "error_message": (
                    getattr(exception, "message", None) or str(exception)
                ),
            }

            # Timeout configuration — all three fields, omitted when None
            if info.schedule_to_close_timeout is not None:
                ctx["schedule_to_close_timeout"] = str(info.schedule_to_close_timeout)
            if info.start_to_close_timeout is not None:
                ctx["start_to_close_timeout"] = str(info.start_to_close_timeout)
            if info.heartbeat_timeout is not None:
                ctx["heartbeat_timeout"] = str(info.heartbeat_timeout)

            # ApplicationError-specific fields survive cross-process serialization
            if isinstance(exception, ApplicationError):
                # The app-set error type string (e.g., "PREFLIGHT_FAILURE",
                # "CircuitBreakerTriggered") — distinct from the Python class name
                if exception.type:
                    ctx["temporal_error_type"] = exception.type
                ctx["non_retryable"] = exception.non_retryable

                # Propagate explicit config_failure tagging from the app.
                # Apps that raise ApplicationError with details[0] = {"config_failure": True, ...}
                # (e.g., preflight_check) get their flag forwarded up the chain.
                if exception.details:
                    try:
                        first_detail = exception.details[0]
                        if isinstance(first_detail, dict) and first_detail.get(
                            "config_failure"
                        ):
                            ctx["config_failure"] = True
                            ctx["config_failure_reason"] = first_detail.get(
                                "config_failure_reason"
                            )
                    except Exception:
                        pass  # details structure unexpected — skip propagation

            with _failure_contexts_lock:
                _failure_contexts[info.workflow_run_id] = ctx

        except Exception as stash_err:
            # Context capture is observability-only — never mask the original exception
            logger.warning(
                f"Failed to stash activity failure context: {stash_err}",
                exc_info=True,
            )


class WorkflowFailureContextWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Workflow interceptor that attaches stashed activity context to terminal failures.

    On workflow failure: pops the activity failure context stashed by the activity
    interceptor and re-raises the exception as ApplicationError with
    details[0] = context. This makes the context readable by the parent workflow
    (AE) via the ChildWorkflowError cause chain without an extra Event History call.

    On workflow success: cleans up any residual stash entry for this run_id.
    When no context was stashed (workflow failure unrelated to an activity, e.g.
    non-determinism error or direct exception in workflow code): re-raises unchanged.

    Sandboxing: must not use threading.Lock — see module docstring.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        run_id = workflow.info().run_id

        try:
            return await super().execute_workflow(input)
        except Exception as e:
            # Only catch Exception (not BaseException) so that CancelledError and
            # Temporal-internal BaseException subclasses (_WorkflowBeingEvictedError
            # etc.) pass through unchanged.  Catching those would break Temporal's
            # workflow eviction and graceful-shutdown mechanisms, causing worker.run()
            # to crash during startup replay or shutdown.
            failure_ctx = _failure_contexts.pop(run_id, None)

            if failure_ctx:
                # Preserve the most useful message and type for the parent
                original_message = (
                    getattr(e, "message", None) or str(e) or type(e).__name__
                )
                # Prefer the Temporal application error type if set, otherwise the
                # Python class name — gives AE the most specific classification signal
                original_type = getattr(e, "type", None) or type(e).__name__

                raise ApplicationError(
                    original_message,
                    failure_ctx,  # details[0] — readable by AE from ChildWorkflowError chain
                    type=original_type,
                    non_retryable=True,  # Terminal workflow failure — retrying the workflow won't help
                ) from e

            raise
        finally:
            # Clean up on success path (already a no-op if except already popped)
            _failure_contexts.pop(run_id, None)


class WorkflowFailureContextInterceptor(Interceptor):
    """Temporal interceptor that propagates activity failure context through child workflows.

    Attaches the failing activity's structured context — activity type, attempt count,
    timeout configuration, error classification, and config_failure flag — to the
    workflow's terminal exception as ApplicationError.details[0].

    This enables the parent Automation Engine workflow to identify exactly which
    activity inside a child workflow caused the failure (and why) without making
    a separate Temporal Event History API call.

    Always-on design: only touches the failure path. The success path is a pure
    pass-through with a single dict.pop() cleanup in the finally block.

    Example — what the parent AE sees when PublishWorkflow fails in bulk_entity_create:

        ChildWorkflowError
          └── cause: ApplicationError(
                  message="ATLAS-404-00-00A: Referenced entity not found",
                  details=[{
                      "activity_type": "bulk_entity_create",
                      "attempt": 3,
                      "task_queue": "atlan-publish-production",
                      "start_to_close_timeout": "0:30:00",
                      "python_error_type": "ApplicationError",
                      "temporal_error_type": "AtlasError",
                      "non_retryable": False,
                  }],
                  type="AtlasError",
              )
                └── cause: original ActivityError / ApplicationError
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Wrap activity execution to capture failure context on every attempt."""
        return WorkflowFailureContextActivityInboundInterceptor(
            super().intercept_activity(next)
        )

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Return the workflow interceptor class that attaches context to failures."""
        return WorkflowFailureContextWorkflowInboundInterceptor
