from pydantic import BaseModel, Field
from temporalio import activity, workflow


class WorkflowContext(BaseModel):
    """Workflow context."""

    in_workflow: str = Field(default="false")
    in_activity: str = Field(default="false")
    workflow_id: str = Field(init=False, default="")
    workflow_type: str = Field(init=False, default="")
    namespace: str = Field(init=False, default="")
    task_queue: str = Field(init=False, default="")
    attempt: str = Field(init=False, default="0")
    activity_id: str = Field(init=False, default="")
    activity_type: str = Field(init=False, default="")
    workflow_run_id: str = Field(init=False, default="")


def get_workflow_context() -> WorkflowContext:
    """Get the workflow context.

    Returns:
        WorkflowContext: The workflow context.
    """

    context = WorkflowContext(in_workflow="false", in_activity="false")

    try:
        workflow_info = workflow.info()
        if workflow_info:
            context.workflow_id = workflow_info.workflow_id or ""
            context.workflow_run_id = workflow_info.run_id or ""
            context.workflow_type = workflow_info.workflow_type or ""
            context.namespace = workflow_info.namespace or ""
            context.task_queue = workflow_info.task_queue or ""
            context.attempt = str(workflow_info.attempt or 0)
            context.in_workflow = "true"
    except Exception:
        pass

    try:
        activity_info = activity.info()
        if activity_info:
            context.in_activity = "true"
            context.workflow_id = activity_info.workflow_id or ""
            context.workflow_run_id = activity_info.workflow_run_id or ""
            context.activity_id = activity_info.activity_id or ""
            context.activity_type = activity_info.activity_type or ""
            context.task_queue = activity_info.task_queue or ""
            context.attempt = str(activity_info.attempt or 0)
    except Exception:
        pass

    return context
