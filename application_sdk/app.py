from typing import Any, Callable, Dict, List, Optional, Type

class WorkflowApp:
    def __init__(
        self,
        application_name: str,
        workflow_classes: List[Type],
        activities: Any,
        workflow_args: Optional[Dict[str, Any]] = None,
    ):
        self.application_name = application_name
        self.workflow_classes = workflow_classes
        self.activities = activities
        self.workflow_args = workflow_args or {}

    async def run(self, workflow_class: Type, daemon: bool = True) -> Any:
        from application_sdk.clients.utils import get_workflow_client
        from application_sdk.worker import Worker

        workflow_client = get_workflow_client(application_name=self.application_name)
        await workflow_client.load()

        worker = Worker(
            workflow_client=workflow_client,
            workflow_classes=self.workflow_classes,
            workflow_activities=workflow_class.get_activities(self.activities),
        )

        workflow_response = await workflow_client.start_workflow(
            self.workflow_args, workflow_class
        )

        await worker.start(daemon=daemon)
        return workflow_response
