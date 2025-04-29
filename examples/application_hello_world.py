import asyncio
from typing import Any, Callable, Dict, Sequence

from temporalio import activity, workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.clients.utils import get_workflow_client
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

APPLICATION_NAME = "hello-world"

logger = get_logger(__name__)


@workflow.defn
class HelloWorldWorkflow(WorkflowInterface):
    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        print("HELLO WORLD")

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        return []


class HelloWorldActivities(ActivitiesInterface):
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        return

    @activity.defn
    @auto_heartbeater
    async def preflight_check(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        return {"message": "Preflight check completed successfully"}


async def application_hello_world(daemon: bool = True) -> Dict[str, Any]:
    print("Starting application_hello_world")

    workflow_client = get_workflow_client(application_name=APPLICATION_NAME)
    await workflow_client.load()

    activities = HelloWorldActivities()

    worker: Worker = Worker(
        workflow_client=workflow_client,
        workflow_classes=[HelloWorldWorkflow],
        workflow_activities=HelloWorldWorkflow.get_activities(activities),
    )

    workflow_response = await workflow_client.start_workflow({}, HelloWorldWorkflow)

    # Start the worker in a separate thread
    await worker.start(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_hello_world(daemon=False))
