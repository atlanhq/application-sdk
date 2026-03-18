"""Client for calling other Apps from within a workflow."""

from typing import TYPE_CHECKING, Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from application_sdk.app.base import _safe_log
    from application_sdk.contracts.base import Input, Output

if TYPE_CHECKING:
    from application_sdk.app.base import App


class WorkflowAppClient:
    """Client for calling other Apps from within a workflow as child workflows."""

    def __init__(self, parent_context_data: dict[str, Any]) -> None:
        """Initialize the client.

        Args:
            parent_context_data: Context from the parent workflow.
        """
        self._parent_context = parent_context_data

    async def call(
        self,
        app_cls: "type[App]",
        input_data: Input,
        *,
        version: str | None = None,  # noqa: ARG002
        parent_context: Any = None,  # noqa: ARG002
        task_queue: str | None = None,
    ) -> Output:
        """Call another App as a child workflow.

        Args:
            app_cls: The App class to call.
            input_data: Input dataclass instance (must extend Input).
            version: Optional version override.
            parent_context: Parent execution context (ignored, uses workflow context).
            task_queue: Task queue to run the child workflow on. If not specified,
                uses the parent's task queue.

        Returns:
            The output from the called App (extends Output).
        """
        # Get app metadata from the class (set by App registration)
        app_name: str = app_cls._app_name
        output_type: type = getattr(app_cls, "_output_type", Output)

        parent_run_id = self._parent_context.get("run_id", "")
        correlation_id = self._parent_context.get("correlation_id", "")

        # Workflow ID: {app}-{config_hash}-{short_id}
        short_id = str(workflow.uuid4()).replace("-", "")[:8]
        child_workflow_id = f"{app_name}-{input_data.config_hash()}-{short_id}"

        # Log child workflow call
        _safe_log(
            "info",
            f"Calling child app | child_app={app_name} workflow_id={child_workflow_id} parent_run_id={parent_run_id} correlation_id={correlation_id} task_queue={task_queue or '(parent)'}",
            child_app=app_name,
            correlation_id=correlation_id,
            parent_run_id=parent_run_id,
        )

        # Use workflow.now() for deterministic timing
        start_time = workflow.now()

        try:
            # Execute as child workflow - pass result_type for proper deserialization
            result: Output = await workflow.execute_child_workflow(
                app_name,  # Use app name as workflow type
                args=[input_data],  # Dataclass directly
                id=child_workflow_id,
                result_type=output_type,
                task_queue=task_queue,  # Route to child's task queue if specified
            )

            # Log completion with duration
            end_time = workflow.now()
            duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)
            _safe_log(
                "info",
                f"Child app call completed | child_app={app_name} duration_ms={duration_ms}",
                child_app=app_name,
                correlation_id=correlation_id,
                duration_ms=duration_ms,
            )

            return result

        except Exception as e:
            _safe_log(
                "error",
                f"Child app call failed | child_app={app_name} error={e} error_type={type(e).__name__}",
                child_app=app_name,
                correlation_id=correlation_id,
                error_type=type(e).__name__,
            )
            raise

    async def call_by_name(
        self,
        app_name: str,
        input_data: Input,
        *,
        version: str | None = None,  # noqa: ARG002
        parent_context: Any = None,  # noqa: ARG002
        output_type: type[Output] | None = None,
        task_queue: str | None = None,
    ) -> "Output | dict[str, Any]":
        """Call another App by name as a child workflow.

        This method enables loose coupling between parent and child workflows.
        The parent workflow doesn't need to import the child's code.

        Args:
            app_name: The registered name of the App.
            input_data: Input dataclass instance (must extend Input).
            version: Optional version override.
            parent_context: Parent execution context (ignored, uses workflow context).
            output_type: Optional Output subclass for deserializing the result.
                        If not provided, returns the result as-is.
            task_queue: Task queue to run the child workflow on.

        Returns:
            The output from the called App. If output_type is provided,
            returns an instance of that type. Otherwise returns the result as-is.
        """
        parent_run_id = self._parent_context.get("run_id", "")
        correlation_id = self._parent_context.get("correlation_id", "")

        # Workflow ID: {app}-{config_hash}-{short_id}
        short_id = str(workflow.uuid4()).replace("-", "")[:8]
        child_workflow_id = f"{app_name}-{input_data.config_hash()}-{short_id}"

        # Log child workflow call
        _safe_log(
            "info",
            f"Calling child app by name | child_app={app_name} workflow_id={child_workflow_id} parent_run_id={parent_run_id} correlation_id={correlation_id} task_queue={task_queue or '(parent)'}",
            child_app=app_name,
            correlation_id=correlation_id,
            parent_run_id=parent_run_id,
        )

        # Use workflow.now() for deterministic timing
        start_time = workflow.now()

        try:
            # Execute as child workflow - Temporal handles dataclass serialization
            result: "Output | dict[str, Any]" = await workflow.execute_child_workflow(
                app_name,  # Use app name as workflow type
                args=[input_data],  # Dataclass directly
                id=child_workflow_id,
                task_queue=task_queue,  # Route to child's task queue if specified
            )

            # Log completion with duration
            end_time = workflow.now()
            duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)
            _safe_log(
                "info",
                f"Child app call completed | child_app={app_name} duration_ms={duration_ms}",
                child_app=app_name,
                correlation_id=correlation_id,
                duration_ms=duration_ms,
            )

            # If output_type provided and result is a dict, deserialize
            if output_type is not None and isinstance(result, dict):
                from dataclasses import fields as dc_fields

                expected_fields = {f.name for f in dc_fields(output_type)}
                filtered_result = {k: v for k, v in result.items() if k in expected_fields}
                return output_type(**filtered_result)

            # Return result as-is (typically the output dataclass)
            return result

        except Exception as e:
            _safe_log(
                "error",
                f"Child app call failed | child_app={app_name} error={e} error_type={type(e).__name__}",
                child_app=app_name,
                correlation_id=correlation_id,
                error_type=type(e).__name__,
            )
            raise
