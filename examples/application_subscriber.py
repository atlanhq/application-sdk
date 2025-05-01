import asyncio
from datetime import timedelta
from typing import Any, Callable, Dict, List

from temporalio import activity, workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.application.fastapi import EventWorkflowTrigger, FastAPIApplication
from application_sdk.clients.constants import TemporalConstants
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.outputs.eventstore import (
    WORKFLOW_END_EVENT,
    AtlanEvent,
    CustomEvent,
    EventStore,
    WorkflowEndEvent,
)
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class SampleActivities(ActivitiesInterface):
    async def _set_state(self, workflow_args: Dict[str, Any]):
        pass

    async def preflight_check(self, workflow_args: Dict[str, Any]) -> None:
        pass

    @activity.defn
    async def activity_1(self):
        logger.info("Activity 1")

        await asyncio.sleep(5)

        # Activities can also send custom events to the event store
        EventStore.create_event(
            event=CustomEvent(data={"custom_key": "custom_value"}),
            topic_name=EventStore.TOPIC_NAME,
        )

        return

    @activity.defn
    async def activity_2(self):
        logger.info("Activity 2")

        await asyncio.sleep(5)

        return


# Workflow that will be triggered by an event
@workflow.defn
class SampleWorkflow(WorkflowInterface):
    activities_cls: type[SampleActivities] = SampleActivities

    @workflow.run
    async def run(self, workflow_config: dict[str, Any]):
        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStoreInput.extract_configuration(
            workflow_id
        )

        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        # When a workflow is triggered by an event, the event is passed in as a dictionary
        event = AtlanEvent(**workflow_args)

        # We can check the event type to determine if the workflow was triggered by an event
        if event.data.event_type != WORKFLOW_END_EVENT:
            return

        # We can also check the event data to get the workflow name and id
        # workflow_end_event: WorkflowEndEvent = event.data
        # workflow_name = workflow_end_event.workflow_name
        # workflow_id = workflow_end_event.workflow_id
        # workflow_output = workflow_end_event.workflow_output

        await workflow.execute_activity_method(
            self.activities_cls.activity_1,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity_method(
            self.activities_cls.activity_2,
            start_to_close_timeout=timedelta(seconds=10),
        )

    @classmethod
    def get_activities(cls, activities: SampleActivities) -> List[Callable[..., Any]]:
        return [activities.activity_1, activities.activity_2]


async def start_worker():
    temporal_client = TemporalClient(
        application_name=TemporalConstants.APPLICATION_NAME.value,
    )
    await temporal_client.load()

    activities = SampleActivities()

    worker = Worker(
        temporal_client=temporal_client,
        temporal_activities=SampleWorkflow.get_activities(activities),
        workflow_classes=[SampleWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    await worker.start(daemon=True)


async def start_fast_api_app():
    temporal_client = TemporalClient(
        application_name=TemporalConstants.APPLICATION_NAME.value,
    )
    await temporal_client.load()

    fast_api_app = FastAPIApplication(
        temporal_client=temporal_client,
    )

    # Register the event trigger to trigger the SampleWorkflow when a dependent workflow ends
    def should_trigger_workflow(event: AtlanEvent) -> bool:
        if event.data.event_type == WORKFLOW_END_EVENT:
            workflow_end_event: WorkflowEndEvent = event.data

            if workflow_end_event.workflow_name != "dependent_workflow":
                return False

            # We can optionally check other attributes of the workflow as well,
            # such as the output of the dependent workflow
            # if workflow_end_event.workflow_output["counter"] > 5:
            #     return False

            return True

        return False

    # Register the event trigger to trigger the SampleWorkflow when a dependent workflow ends
    fast_api_app.register_workflow(
        SampleWorkflow,
        triggers=[
            EventWorkflowTrigger(
                should_trigger_workflow=should_trigger_workflow,
                workflow_class=SampleWorkflow,
            )
        ],
    )

    await fast_api_app.start()


async def simulate_worklflow_end_event():
    await asyncio.sleep(5)

    # Simulates that a dependent workflow has ended
    EventStore.create_event(
        event=WorkflowEndEvent(
            workflow_name="dependent_workflow",
            workflow_id="test",
            workflow_run_id="test",
            workflow_output={"output_value": 0},
        ),
        topic_name=EventStore.TOPIC_NAME,
    )


async def application_subscriber():
    # Start the worker
    await start_worker()

    # Start the workflow and the fast api app
    ## We start the FastAPI app first, so that it can listen for events
    ## We regsiter an event trigger in the FastAPI app, so that it can trigger the SampleWorkflow
    ## When the dependent workflow ends, it will trigger the SampleWorkflow
    await asyncio.gather(simulate_worklflow_end_event(), start_fast_api_app())


if __name__ == "__main__":
    asyncio.run(application_subscriber())
