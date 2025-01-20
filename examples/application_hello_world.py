import asyncio
import logging
import time
from typing import Any, Dict
from urllib.parse import quote_plus

from temporalio import workflow

from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.clients.sql import AsyncSQLClient
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers.sql import SQLHandler
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import (
    SQLMetadataExtractionWorkflow,
)

APPLICATION_NAME = "hello-world"

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class CustomSQLClient(AsyncSQLClient):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password: str = quote_plus(self.credentials["password"])
        return f"driver+psycopg://{self.credentials['user']}:{encoded_password}@{self.credentials['host']}:{self.credentials['port']}/{self.credentials['database']}"


class SampleSQLActivities(SQLMetadataExtractionActivities):
    fetch_database_sql = ""
    fetch_schema_sql = ""
    fetch_table_sql = ""
    fetch_column_sql = ""


class SampleSQLWorkflowHandler(SQLHandler):
    tables_check_sql = ""
    metadata_sql = ""


@workflow.defn
class SampleSQLWorkflow(SQLMetadataExtractionWorkflow):
    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]) -> None:
        print("HELLO WORLD")


async def application_hello_world() -> None:
    print("Starting application_sql")

    temporal_client = TemporalClient(
        application_name=APPLICATION_NAME,
    )
    await temporal_client.load()

    activities = SampleSQLActivities(
        sql_client_class=CustomSQLClient, handler_class=SampleSQLWorkflowHandler
    )

    worker: Worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[SampleSQLWorkflow],
        temporal_activities=SampleSQLWorkflow.get_activities(activities),
    )

    # Start the worker in a separate thread
    await worker.start(daemon=True)

    # wait for the worker to start
    time.sleep(3)

    workflow_args = {
        "credentials": {},
        "connection": {"connection": "dev"},
        "metadata": {},
        "tenant_id": "123",
    }

    await temporal_client.start_workflow(workflow_args, SampleSQLWorkflow)


if __name__ == "__main__":
    asyncio.run(application_hello_world())
    time.sleep(1000000)
