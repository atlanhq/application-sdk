import asyncio
import logging
import threading
import time
from datetime import timedelta
from typing import Any, Callable, List

from temporalio import activity, workflow

from application_sdk.app.rest.fastapi import EventWorkflowTrigger, FastAPIApplication
from application_sdk.clients.constants import TemporalConstants
from application_sdk.clients.temporal_client import TemporalClient, TemporalConfig
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore
from application_sdk.paas.eventstore import EventStore
from application_sdk.paas.eventstore.models import (
    WORKFLOW_END_EVENT,
    AtlanEvent,
    CustomEvent,
    WorkflowEndEvent,
)
from application_sdk.workflows.builder import WorkflowBuilderInterface
from application_sdk.workflows.workers.worker import WorkflowWorker
from application_sdk.workflows.workflow import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


# Workflow that will be triggered by an event
@workflow.defn
class SampleWorkflow(WorkflowInterface):
    @activity.defn
    async def set_workflow_activity_context(self, workflow_id: str) -> dict[str, Any]:
        """
        As we use a single worker thread, we need to set the workflow activity context
        """
        workflow_args = StateStore.extract_configuration(workflow_id)

        return workflow_args

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

    @workflow.run
    async def run(self, workflow_config: dict[str, Any]):
        workflow_id = workflow_config["workflow_id"]

        # dedicated activity to set the workflow activity context
        workflow_args: dict[str, Any] = await workflow.execute_activity(
            self.set_workflow_activity_context,
            workflow_id,
            start_to_close_timeout=timedelta(seconds=1000),
        )

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

        await workflow.execute_activity(
            self.activity_1,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            self.activity_2,
            start_to_close_timeout=timedelta(seconds=10),
        )

    async def start(self, workflow_args: Any, workflow_class: Any):
        return await super().start(workflow_args, self.__class__)

    def get_activities(self) -> List[Callable[..., Any]]:
        return [self.activity_1, self.activity_2, self.set_workflow_activity_context]


class SampleWorkflowBuilder(WorkflowBuilderInterface):
    temporal_client: TemporalClient

    def set_temporal_client(
        self, temporal_client: TemporalClient
    ) -> "SampleWorkflowBuilder":
        self.temporal_client = temporal_client
        return self

    def build(self, workflow: SampleWorkflow | None = None) -> WorkflowInterface:
        return SampleWorkflow().set_temporal_client(self.temporal_client)


async def start_worker():
    temporal_client = TemporalClient(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_client.load()

    workflow: WorkflowInterface = (
        SampleWorkflowBuilder().set_temporal_client(temporal_client).build()
    )

    worker: WorkflowWorker = WorkflowWorker(
        temporal_client=temporal_client,
        temporal_activities=workflow.get_activities(),
        workflow_classes=[SampleWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()
    time.sleep(3)


async def start_fast_api_app():
    fast_api_app = FastAPIApplication()

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

    temporal_client = TemporalClient(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_client.load()
    sample_worflow = (
        SampleWorkflowBuilder().set_temporal_client(temporal_client).build()
    )

    # Register the event trigger to trigger the SampleWorkflow when a dependent workflow ends
    fast_api_app.register_workflow(
        sample_worflow,
        triggers=[
            EventWorkflowTrigger(
                should_trigger_workflow=should_trigger_workflow,
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
