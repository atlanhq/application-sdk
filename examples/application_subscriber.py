import asyncio
import threading
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List

from temporalio import activity, workflow

from application_sdk.app.rest.fastapi import FastAPIApplication
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
    async def activity_1(self, workflow_args: Any):
        print("Activity 1")
        return

    @activity.defn
    async def activity_2(self, workflow_args: Any):
        print("Activity 2")
        return

    @workflow.run
    async def run(self, workflow_args: Any):
        await workflow.execute_activity(
            self.activity_1,
            workflow_args,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            self.activity_2,
            workflow_args,
            start_to_close_timeout=timedelta(seconds=10),
        )

    async def start(self, workflow_args: Any, workflow_class: Any):
        return await super().start(workflow_args, self.__class__)

    def get_activities(self) -> List[Callable[..., Any]]:
        return [self.activity_1, self.activity_2]


class SampleWorkflowBuilder(WorkflowBuilderInterface):
    temporal_resource: TemporalResource

    def build(self, workflow: SampleWorkflow | None = None) -> WorkflowInterface:
        return SampleWorkflow().set_temporal_resource(self.temporal_resource)


class SampleFastAPIApplication(FastAPIApplication):
    def register_routes(self):
        super().register_routes()

        self.app.add_api_route(
            "/setup_trigger",
            self.setup_trigger,
            methods=["POST"],
        )

    async def setup_trigger(self):
        def should_trigger_workflow(workflow_input: Any) -> bool:
            return True

        return self.register_event_trigger(SampleWorkflow, should_trigger_workflow)


async def start_fast_api_app():
    fast_api_app = SampleFastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SQLWorkflow(),
    )

    def should_trigger_workflow(event: Any) -> bool:
        return True

    fast_api_app.register_event_trigger(SampleWorkflow, should_trigger_workflow)

    await fast_api_app.start()


async def start_workflow():
    await asyncio.sleep(5)

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

    # wait for the worker to start
    time.sleep(3)

    return await workflow.start({}, SampleWorkflow)


async def main():
    await asyncio.gather(start_workflow(), start_fast_api_app())


if __name__ == "__main__":
    asyncio.run(main())
