import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Type

from temporalio import activity, workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.application import BaseApplication
from application_sdk.clients.utils import get_workflow_client
from application_sdk.constants import APPLICATION_NAME
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.eventstore import (
    EventMetadata,
    EventStore,
    WorkflowEndEvent,
    WorkflowStates,
)
from application_sdk.server.fastapi.models import WorkflowEndEventTrigger
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class SampleActivities(ActivitiesInterface):
    @activity.defn
    async def activity_1(self):
        logger.info("Activity 1")

        await asyncio.sleep(5)

        return

    @activity.defn
    async def activity_2(self):
        logger.info("Activity 2")

        await asyncio.sleep(5)

        return


# Workflow that will be triggered by an event
@workflow.defn
class SampleWorkflow(WorkflowInterface):
    activities_cls: Type[ActivitiesInterface] = SampleActivities

    @workflow.run
    async def run(self, workflow_config: dict[str, Any]):
        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStoreInput.extract_configuration(
            workflow_id
        )

        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        # When a workflow is triggered by an event, the event is passed in as a dictionary
        event = WorkflowEndEvent(**workflow_args["data"])

        # We can also check the event data to get the workflow name and id
        workflow_name = event.metadata.workflow_name
        workflow_id = event.metadata.workflow_id
        workflow_output = event.workflow_output

        print("workflow_name", workflow_name)
        print("workflow_id", workflow_id)
        print("workflow_output", workflow_output)

        await workflow.execute_activity_method(
            self.activities_cls.activity_1,
            start_to_close_timeout=timedelta(seconds=10),
            heartbeat_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity_method(
            self.activities_cls.activity_2,
            start_to_close_timeout=timedelta(seconds=10),
            heartbeat_timeout=timedelta(seconds=10),
        )

    @staticmethod
    def get_activities(activities: SampleActivities) -> List[Callable[..., Any]]:
        return [activities.activity_1, activities.activity_2]


async def start_worker():
    workflow_client = get_workflow_client(
        application_name=APPLICATION_NAME,
    )
    await workflow_client.load()

    activities = SampleActivities()

    worker = Worker(
        workflow_client=workflow_client,
        workflow_activities=SampleWorkflow.get_activities(activities),
        workflow_classes=[SampleWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    await worker.start(daemon=True)


async def application_subscriber():
    application = BaseApplication(
        name=APPLICATION_NAME,
    )

    await application.setup_workflow(
        workflow_classes=[SampleWorkflow], activities_class=SampleActivities
    )

    await application.start_worker()

    await application.setup_server(
        workflow_class=SampleWorkflow,
        ui_enabled=False,
        triggers=[
            WorkflowEndEventTrigger(
                finished_workflow_name="dependent_workflow",
                finished_workflow_state=WorkflowStates.COMPLETED.value,
            )
        ],
    )

    await asyncio.gather(application.start_server(), simulate_worklflow_end_event())


async def simulate_worklflow_end_event():
    await asyncio.sleep(15)

    # Simulates that a dependent workflow has ended
    EventStore.publish_event(
        event=WorkflowEndEvent(
            metadata=EventMetadata(
                workflow_name="dependent_workflow",
                workflow_state=WorkflowStates.COMPLETED.value,
                workflow_id="123",
                workflow_run_id="456",
                application_name=APPLICATION_NAME,
                event_published_client_timestamp=int(datetime.now().timestamp()),
            )
        ),
    )


if __name__ == "__main__":
    asyncio.run(application_subscriber())
