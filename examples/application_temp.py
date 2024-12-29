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
from application_sdk.paas.eventstore.models import DaprEvent, WorkflowEndEvent


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
        # counter = workflow_args["counter"]
        # counter += 1

        # await workflow.execute_activity(
        #     self.activity_1,
        #     workflow_args,
        #     start_to_close_timeout=timedelta(seconds=10),
        # )
        # await workflow.execute_activity(
        #     self.activity_2,
        #     workflow_args,
        #     start_to_close_timeout=timedelta(seconds=10),
        # )

        import os
        print("HELELO", os.getenv("SHELL"))

        # return {
        #     "counter": counter
        # }

    async def start(self, workflow_args: Any, workflow_class: Any):
        return await super().start(workflow_args, self.__class__)

    def get_activities(self) -> List[Callable[..., Any]]:
        return [self.activity_1, self.activity_2]



async def start_workflow():
    temporal_resource = TemporalResource(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_resource.load()
    workflow = SampleWorkflow()

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

    # await worker.start()

    # wait for the worker to start
    time.sleep(3)

    return await workflow.start({
        "counter": 0
    }, SampleWorkflow)


async def main():
    await start_workflow()


if __name__ == "__main__":
    asyncio.run(main())
