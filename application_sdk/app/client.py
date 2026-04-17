"""Client for calling other Apps from within a workflow."""

# BLDX-878: inter-app calls deactivated pending review.
# The WorkflowAppClient class and its call() / call_by_name() methods are
# commented out below.  To re-enable, remove this block comment and restore
# the class, then undo the corresponding edits in app/base.py and
# tests/unit/app/test_client.py.

# from typing import TYPE_CHECKING, Any
#
# from temporalio import workflow
#
# with workflow.unsafe.imports_passed_through():
#     from application_sdk.app.base import _safe_log
#     from application_sdk.contracts.base import Input, Output
#
# if TYPE_CHECKING:
#     from application_sdk.app.base import App
#
#
# class WorkflowAppClient:
#     """Client for calling other Apps from within a workflow as child workflows."""
#
#     def __init__(self, parent_context_data: dict[str, Any]) -> None:
#         self._parent_context = parent_context_data
#
#     async def call(
#         self,
#         app_cls: "type[App]",
#         input_data: Input,
#         *,
#         version: str | None = None,  # noqa: ARG002
#         parent_context: Any = None,  # noqa: ARG002
#         task_queue: str | None = None,
#     ) -> Output:
#         app_name: str = app_cls._app_name
#         output_type: type = getattr(app_cls, "_output_type", Output)
#
#         parent_run_id = self._parent_context.get("run_id", "")
#         correlation_id = self._parent_context.get("correlation_id", "")
#
#         short_id = str(workflow.uuid4()).replace("-", "")[:8]
#         child_workflow_id = f"{app_name}-{input_data.config_hash()}-{short_id}"
#
#         _safe_log(
#             "info",
#             f"Calling child app | child_app={app_name} workflow_id={child_workflow_id} parent_run_id={parent_run_id} correlation_id={correlation_id} task_queue={task_queue or '(parent)'}",
#             child_app=app_name,
#             correlation_id=correlation_id,
#             parent_run_id=parent_run_id,
#         )
#
#         start_time = workflow.now()
#
#         try:
#             result: Output = await workflow.execute_child_workflow(
#                 app_name,
#                 args=[input_data],
#                 id=child_workflow_id,
#                 result_type=output_type,
#                 task_queue=task_queue,
#             )
#
#             end_time = workflow.now()
#             duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)
#             _safe_log(
#                 "info",
#                 f"Child app call completed | child_app={app_name} duration_ms={duration_ms}",
#                 child_app=app_name,
#                 correlation_id=correlation_id,
#                 duration_ms=duration_ms,
#             )
#
#             return result
#
#         except Exception as e:
#             _safe_log(
#                 "error",
#                 f"Child app call failed | child_app={app_name} error={e} error_type={type(e).__name__}",
#                 child_app=app_name,
#                 correlation_id=correlation_id,
#                 error_type=type(e).__name__,
#             )
#             raise
#
#     async def call_by_name(
#         self,
#         app_name: str,
#         input_data: Input,
#         *,
#         version: str | None = None,  # noqa: ARG002
#         parent_context: Any = None,  # noqa: ARG002
#         output_type: type[Output] | None = None,
#         task_queue: str | None = None,
#     ) -> "Output | dict[str, Any]":
#         parent_run_id = self._parent_context.get("run_id", "")
#         correlation_id = self._parent_context.get("correlation_id", "")
#
#         short_id = str(workflow.uuid4()).replace("-", "")[:8]
#         child_workflow_id = f"{app_name}-{input_data.config_hash()}-{short_id}"
#
#         _safe_log(
#             "info",
#             f"Calling child app by name | child_app={app_name} workflow_id={child_workflow_id} parent_run_id={parent_run_id} correlation_id={correlation_id} task_queue={task_queue or '(parent)'}",
#             child_app=app_name,
#             correlation_id=correlation_id,
#             parent_run_id=parent_run_id,
#         )
#
#         start_time = workflow.now()
#
#         try:
#             result: "Output | dict[str, Any]" = await workflow.execute_child_workflow(
#                 app_name,
#                 args=[input_data],
#                 id=child_workflow_id,
#                 task_queue=task_queue,
#             )
#
#             end_time = workflow.now()
#             duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)
#             _safe_log(
#                 "info",
#                 f"Child app call completed | child_app={app_name} duration_ms={duration_ms}",
#                 child_app=app_name,
#                 correlation_id=correlation_id,
#                 duration_ms=duration_ms,
#             )
#
#             if output_type is not None and isinstance(result, dict):
#                 known = set(output_type.model_fields)
#                 return output_type.model_validate(
#                     {k: v for k, v in result.items() if k in known}
#                 )
#
#             return result
#
#         except Exception as e:
#             _safe_log(
#                 "error",
#                 f"Child app call failed | child_app={app_name} error={e} error_type={type(e).__name__}",
#                 child_app=app_name,
#                 correlation_id=correlation_id,
#                 error_type=type(e).__name__,
#             )
#             raise
