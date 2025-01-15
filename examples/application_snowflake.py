from application_sdk.worker import Worker
from application_sdk.workflows.resources.temporal_resource import TemporalResource, TemporalConfig
from application_sdk.workflows.metadata_extraction.sql.workflow import SQLMetadataExtractionWorkflow
from application_sdk.activities.metadata_extraction.sql import SQLExtractionActivities
import threading
from typing import Type, Dict, Any
import asyncio
from application_sdk.activities.metadata_extraction.sql import SQLClient, SQLHandler
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from datetime import timedelta

class SnowflakeClient(SQLClient):
    def __init__(self):
        pass

class SnowflakeHandler(SQLHandler):
    def __init__(self):
        pass

class SnowflakeActivities(SQLExtractionActivities):
    sql_client_class = SnowflakeClient
    handler_class = SnowflakeHandler

    @activity.defn
    async def fetch_warehouses(self, workflow_args: Dict[str, Any]):
        pass

    @activity.defn
    async def fetch_pipes(self, workflow_args: Dict[str, Any]):
        pass

    @activity.defn
    async def fetch_streams(self, workflow_args: Dict[str, Any]):
        pass

@workflow.defn
class SnowflakeWorkflow(SQLMetadataExtractionWorkflow):
    activities_cls: Type[SnowflakeActivities] = SnowflakeActivities

    def __init__(self, activities_cls: Type[SnowflakeActivities] = SnowflakeActivities):
        super().__init__(activities_cls=activities_cls)

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        await workflow.execute_activity_method(
            self.activities_cls.fetch_warehouses,
            args=[workflow_config],
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )
        


async def start_worker(temporal_resource: TemporalResource, workflow: SnowflakeWorkflow):
    activities = SnowflakeActivities()

    worker: Worker = Worker(
        temporal_resource=temporal_resource,
        temporal_activities=[activities.fetch_warehouses, activities.fetch_pipes, activities.fetch_streams],
        workflow_classes=[SnowflakeWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()

async def start_workflow(temporal_resource: TemporalResource, workflow_cls: Type[SQLMetadataExtractionWorkflow]):
    await asyncio.sleep(5)

    await temporal_resource.start_workflow(
        workflow_args={
            "credentials": {
                "username": "test",
                "password": "test",
            },
        },
        workflow_class=workflow_cls,
    )

async def main():
    temporal_resource = TemporalResource(temporal_config=TemporalConfig())
    await temporal_resource.load()

    workflow = SnowflakeWorkflow()

    await start_worker(temporal_resource, workflow)
    await start_workflow(temporal_resource, SnowflakeWorkflow)

    await asyncio.sleep(1000000)

if __name__ == "__main__":
    asyncio.run(main())
