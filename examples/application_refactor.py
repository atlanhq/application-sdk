from application_sdk.worker import Worker
from application_sdk.workflows.resources.temporal_resource import TemporalResource, TemporalConfig
from application_sdk.workflows.metadata_extraction.sql.workflow import SQLMetadataExtractionWorkflow
from application_sdk.activities.metadata_extraction.sql import SQLExtractionActivities
import threading
from typing import Type
import asyncio

async def start_worker(temporal_resource: TemporalResource, workflow: SQLMetadataExtractionWorkflow):
    activities = SQLExtractionActivities()

    worker: Worker = Worker(
        temporal_resource=temporal_resource,
        temporal_activities=[activities.fetch_databases, activities.fetch_tables, activities.fetch_columns, activities.transform_metadata, activities.write_metadata],
        workflow_classes=[SQLMetadataExtractionWorkflow],
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

    workflow = SQLMetadataExtractionWorkflow()

    await start_worker(temporal_resource, workflow)
    await start_workflow(temporal_resource, SQLMetadataExtractionWorkflow)

    await asyncio.sleep(1000000)

if __name__ == "__main__":
    asyncio.run(main())
