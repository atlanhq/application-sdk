import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Type, cast

from dapr import clients
from temporalio import activity, workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.application import BaseApplication
from application_sdk.clients.utils import get_workflow_client
from application_sdk.constants import APPLICATION_NAME
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.eventstore import (
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventStore,
    EventTypes,
    ObservabilityEventNames,
    WorkflowStates,
)
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class SampleActivities(ActivitiesInterface):
    @activity.defn
    async def activity_1(self):
        logger.info("Activity 1")

        EventStore.publish_event(
            event=Event(
                event_type=EventTypes.OBSERVABILITY_EVENT.value,
                event_name=ObservabilityEventNames.ERROR.value,
                data={
                    "error_message": "This is a test error",
                    "error_stack_trace": "This is a test stack trace",
                },
            )
        )

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
        # Get the workflow configuration from the state store
        workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
            self.activities_cls.get_workflow_args,
            workflow_config,  # Pass the whole config containing workflow_id
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        # When a workflow is triggered by an event, the event is passed in as a dictionary
        event = Event(**workflow_args["event"])

        # We can also check the event data to get the workflow name and id
        workflow_type = event.metadata.workflow_type
        workflow_id = event.metadata.workflow_id

        print("workflow_type", workflow_type)
        print("workflow_id", workflow_id)

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
    def get_activities(activities: ActivitiesInterface) -> List[Callable[..., Any]]:
        sample_activities = cast(SampleActivities, activities)

        return [
            sample_activities.activity_1,
            sample_activities.activity_2,
            sample_activities.get_workflow_args,
        ]


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
    # Open the application manifest in the current directory
    application_manifest = json.load(
        open(os.path.join(os.path.dirname(__file__), "subscriber_manifest.json"))
    )

    # 2 Steps to setup event registration,
    # 1. Setup the event in the manifest, with event name, type and filters => This creates an event trigger
    # 2. Register the event subscription to a workflow => This binds the workflow to the event trigger

    # Initialize the application
    application = BaseApplication(
        name=APPLICATION_NAME,
        application_manifest=application_manifest,  # Optional, if the manifest has event registration, it will be bootstrapped
    )

    # Register the event subscription to a workflow
    application.register_event_subscription("AssetExtractionCompleted", SampleWorkflow)

    # Can also register the events to multiple workflows
    # application.register_event_subscription("ErrorEvent", SampleWorkflow)

    # Setup the workflow is needed to start the worker
    await application.setup_workflow(
        workflow_classes=[SampleWorkflow], activities_class=SampleActivities
    )
    await application.start_worker()

    await application.setup_server(
        workflow_class=SampleWorkflow,
        ui_enabled=False,
    )

    await asyncio.gather(application.start_server(), simulate_worklflow_end_event())


async def simulate_worklflow_end_event():
    await asyncio.sleep(15)

    # Simulates that a dependent workflow has ended
    event = Event(
        metadata=EventMetadata(
            workflow_type="AssetExtractionWorkflow",
            workflow_state=WorkflowStates.COMPLETED.value,
            workflow_id="123",
            workflow_run_id="456",
            application_name="AssetExtractionApplication",
            event_published_client_timestamp=int(datetime.now().timestamp()),
        ),
        event_type=EventTypes.APPLICATION_EVENT.value,
        event_name=ApplicationEventNames.WORKFLOW_END.value,
        data={},
    )
    with clients.DaprClient() as client:
        client.publish_event(
            pubsub_name=EventStore.EVENT_STORE_NAME,
            topic_name=event.get_topic_name(),
            data=json.dumps(event.model_dump(mode="json")),
            data_content_type="application/json",
        )


if __name__ == "__main__":
    asyncio.run(application_subscriber())
