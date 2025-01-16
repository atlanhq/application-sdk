import asyncio
import threading
from datetime import timedelta
from typing import Any, Dict, Type

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.metadata_extraction.sql import (
    SQLClient,
    SQLExtractionActivities,
    SQLHandler,
)
from application_sdk.clients.temporal_client import TemporalClient, TemporalConfig
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql.workflow import (
    SQLMetadataExtractionWorkflow,
)


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

    fetch_warehouses_sql: str = ""
    fetch_pipes_sql: str = ""

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


async def start_worker(temporal_client: TemporalClient, workflow: SnowflakeWorkflow):
    activities = SnowflakeActivities()

    worker: Worker = Worker(
        temporal_client=temporal_client,
        temporal_activities=[
            activities.fetch_warehouses,
            activities.fetch_pipes,
            activities.fetch_streams,
        ],
        workflow_classes=[SnowflakeWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()


async def start_workflow(
    temporal_client: TemporalClient,
    workflow_cls: Type[SQLMetadataExtractionWorkflow],
):
    await asyncio.sleep(5)

    await temporal_client.start_workflow(
        workflow_args={
            "credentials": {
                "username": "test",
                "password": "test",
            },
        },
        workflow_class=workflow_cls,
    )


async def main():
    temporal_client = TemporalClient(temporal_config=TemporalConfig())
    await temporal_client.load()

    await start_worker(temporal_client, SnowflakeWorkflow)
    await start_workflow(temporal_client, SnowflakeWorkflow)

    await asyncio.sleep(1000000)


if __name__ == "__main__":
    asyncio.run(main())
