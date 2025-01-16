import asyncio
import os
import threading
from typing import Any, Dict, Type

from temporalio import workflow

from application_sdk.activities.metadata_extraction.sql import SQLExtractionActivities
from application_sdk.clients.temporal_client import TemporalClient, TemporalConfig
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql.workflow import (
    SQLMetadataExtractionWorkflow,
)


@workflow.defn
class PostgresWorkflow(SQLMetadataExtractionWorkflow):
    fetch_database_sql: str = "SELECT * FROM information_schema.tables"
    fetch_schema_sql: str = "SELECT * FROM information_schema.tables"
    fetch_table_sql: str = "SELECT * FROM information_schema.tables"
    fetch_column_sql: str = "SELECT * FROM information_schema.tables"

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]):
        await super().run(workflow_config)


async def start_worker(temporal_client: TemporalClient):
    activities = SQLExtractionActivities()

    worker: Worker = Worker(
        temporal_client=temporal_client,
        temporal_activities=SQLMetadataExtractionWorkflow.get_activities(activities),
        workflow_classes=[SQLMetadataExtractionWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()


async def start_workflow(
    temporal_client: TemporalClient, workflow_cls: Type[SQLMetadataExtractionWorkflow]
):
    await asyncio.sleep(5)

    await temporal_client.start_workflow(
        workflow_args={
            "credentials": {
                "host": os.getenv("POSTGRES_HOST", "localhost"),
                "port": os.getenv("POSTGRES_PORT", "5432"),
                "user": os.getenv("POSTGRES_USER", "postgres"),
                "password": os.getenv("POSTGRES_PASSWORD", "password"),
                "database": os.getenv("POSTGRES_DATABASE", "postgres"),
            },
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude_filter": "{}",
                "include_filter": "{}",
                "temp_table_regex": "",
                "advanced_config_strategy": "default",
                "use_source_schema_filtering": "false",
                "use_jdbc_internal_methods": "true",
                "authentication": "BASIC",
                "extraction-method": "direct",
                "exclude_views": "true",
                "exclude_empty_tables": "false",
            },
            "tenant_id": "123",
        },
        workflow_class=workflow_cls,
    )


async def main():
    temporal_client = TemporalClient(temporal_config=TemporalConfig())
    await temporal_client.load()

    await start_worker(temporal_client)
    await start_workflow(temporal_client, SQLMetadataExtractionWorkflow)

    await asyncio.sleep(1000000)


if __name__ == "__main__":
    asyncio.run(main())
