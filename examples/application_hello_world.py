import asyncio
import logging
from typing import Any, Callable, Dict, Sequence

from temporalio import workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

APPLICATION_NAME = "hello-world"

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


@workflow.defn
class HelloWorldWorkflow(WorkflowInterface):
    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]) -> None:
        print("HELLO WORLD")

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        return []


class HelloWorldActivities(ActivitiesInterface):
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        return

    async def preflight_check(self, workflow_args: Dict[str, Any]) -> None:
        return


async def application_hello_world(daemon: bool = True) -> Dict[str, Any]:
    print("Starting application_hello_world")

    temporal_client = TemporalClient(
        application_name=APPLICATION_NAME,
    )
    await temporal_client.load()

    activities = HelloWorldActivities()

    worker: Worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[HelloWorldWorkflow],
        temporal_activities=HelloWorldWorkflow.get_activities(activities),
    )

    workflow_response = await temporal_client.start_workflow({}, HelloWorldWorkflow)

    # Start the worker in a separate thread
    await worker.start(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_hello_world(daemon=False))
