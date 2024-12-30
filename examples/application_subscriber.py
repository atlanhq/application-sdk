import asyncio
import logging
import threading
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List

from temporalio import activity, workflow

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.paas.eventstore import EventStore
from application_sdk.paas.eventstore.models import (
    WORKFLOW_END_EVENT,
    CustomEvent,
    DaprEvent,
    WorkflowEndEvent,
)
from application_sdk.workflows.builder import WorkflowBuilderInterface
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.resources.constants import TemporalConstants
from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.workers.worker import WorkflowWorker
from application_sdk.workflows.workflow import WorkflowInterface

logger = logging.getLogger(__name__)


# Workflow
class WorkflowAuthController(WorkflowAuthControllerInterface):
    async def prepare(self, credentials: Dict[str, Any]) -> None:
        pass

    async def test_auth(self) -> bool:
        return True


class WorkflowMetadataController(WorkflowMetadataControllerInterface):
    async def prepare(self, credentials: Dict[str, Any]) -> None:
        pass

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        return [{"database": "test", "schema": "test"}]


class WorkflowPreflightCheckController(WorkflowPreflightCheckControllerInterface):
    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": True,
            "data": {
                "databaseSchemaCheck": {
                    "success": True,
                    "successMessage": "Schemas and Databases check successful",
                    "failureMessage": "",
                },
                "tablesCheck": {
                    "success": True,
                    "successMessage": "Tables check successful. Table count: 2",
                    "failureMessage": "",
                },
            },
        }


@workflow.defn
class SampleWorkflow(WorkflowInterface):
    @activity.defn
    async def activity_1(self):
        logger.info("Activity 1")

        EventStore.create_custom_event(
            event=CustomEvent(data={"custom_key": "custom_value"}),
            topic_name=EventStore.TOPIC_NAME,
        )

        return

    @activity.defn
    async def activity_2(self):
        logger.info("Activity 2")
        return

    @workflow.run
    async def run(self, workflow_args: dict[str, Any]):
        event = DaprEvent(**workflow_args)

        if event.data.event_type != WORKFLOW_END_EVENT:
            return

        workflow_end_event: WorkflowEndEvent = event.data
        counter = workflow_end_event.workflow_output["counter"]

        counter += 1

        await workflow.execute_activity(
            self.activity_1,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            self.activity_2,
            start_to_close_timeout=timedelta(seconds=10),
        )

        return {"counter": counter}

    async def start(self, workflow_args: Any, workflow_class: Any):
        return await super().start(workflow_args, self.__class__)

    def get_activities(self) -> List[Callable[..., Any]]:
        return [self.activity_1, self.activity_2]


class SampleWorkflowBuilder(WorkflowBuilderInterface):
    temporal_resource: TemporalResource

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "SampleWorkflowBuilder":
        self.temporal_resource = temporal_resource
        return self

    def build(self, workflow: SampleWorkflow | None = None) -> WorkflowInterface:
        return SampleWorkflow().set_temporal_resource(self.temporal_resource)


async def start_worker():
    temporal_resource = TemporalResource(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_resource.load()

    workflow: WorkflowInterface = (
        SampleWorkflowBuilder().set_temporal_resource(temporal_resource).build()
    )

    worker: WorkflowWorker = WorkflowWorker(
        temporal_resource=temporal_resource,
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
    fast_api_app = FastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SQLWorkflow(),
    )

    # Register the event trigger to trigger the SampleWorkflow when a dependent workflow ends
    def should_trigger_workflow(event: DaprEvent) -> bool:
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
    temporal_resource = TemporalResource(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_resource.load()
    sample_worflow = (
        SampleWorkflowBuilder().set_temporal_resource(temporal_resource).build()
    )
    fast_api_app.register_event_trigger(sample_worflow, should_trigger_workflow)

    await fast_api_app.start()


async def simulate_worklflow_end_event():
    await asyncio.sleep(5)

    # Simulates that a dependent workflow has ended
    EventStore.create_workflow_end_event(
        event=WorkflowEndEvent(
            workflow_name="dependent_workflow",
            workflow_id="test",
            workflow_run_id="test",
            workflow_output={"counter": 0},
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
