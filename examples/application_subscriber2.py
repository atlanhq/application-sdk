import asyncio
import os
import threading
import time
from typing import Any, Dict, List
from urllib.parse import quote_plus

from application_sdk.app.rest.fastapi import FastAPIApplication
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
from application_sdk.workflows.sql.builders.builder import SQLWorkflowBuilder
from application_sdk.workflows.sql.resources.async_sql_resource import AsyncSQLResource
from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers.atlas import AtlasTransformer
from application_sdk.workflows.workers.worker import WorkflowWorker


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


class PostgreSQLResource(AsyncSQLResource):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password: str = quote_plus(self.config.credentials["password"])
        return f"postgresql+psycopg://{self.config.credentials['user']}:{encoded_password}@{self.config.credentials['host']}:{self.config.credentials['port']}/{self.config.credentials['database']}"


class SampleSQLWorkflow(SQLWorkflow):
    fetch_database_sql = """
    SELECT datname as database_name FROM pg_database WHERE datname = current_database();
    """

    fetch_schema_sql = """
    SELECT
        s.*
    FROM
        information_schema.schemata s
    WHERE
        s.schema_name NOT LIKE 'pg_%'
        AND s.schema_name != 'information_schema'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';
    """

    fetch_table_sql = """
    SELECT
        t.*
    FROM
        information_schema.tables t
    WHERE
        concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
        AND t.table_name !~ '{exclude_table}';
    """

    fetch_column_sql = """
    SELECT
        c.*
    FROM
        information_schema.columns c
    WHERE
        concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
        AND c.table_name !~ '{exclude_table}';
    """

    sql_resource: SQLResource | None = PostgreSQLResource(SQLResourceConfig())


class SampleSQLWorkflowBuilder(SQLWorkflowBuilder):
    def build(self, workflow: SQLWorkflow | None = None) -> SQLWorkflow:
        return super().build(workflow=workflow or SampleSQLWorkflow())


class SampleFastAPIApplication(FastAPIApplication):
    async def on_activity_start(self, event: dict):
        print(f"EVENT: Activity start: {event}")
        return {}

    async def on_activity_end(self, event: dict):
        print(f"EVENT: Activity end: {event}")
        return {}

    async def on_workflow_start(self, event: dict):
        print(f"EVENT: Workflow start: {event}")
        return {}

    async def on_workflow_end(self, event: dict):
        print(f"EVENT: Workflow end: {event}")
        return {}


async def start_fast_api_app():
    fast_api_app = SampleFastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SampleSQLWorkflow(),
    )
    fast_api_app.subscribe_to_workflow_end(
        app_name=TemporalConstants.APPLICATION_NAME.value
    )
    fast_api_app.subscribe_to_workflow_start(
        app_name=TemporalConstants.APPLICATION_NAME.value
    )

    await fast_api_app.start()


async def start_workflow():
    await asyncio.sleep(10)

    temporal_resource = TemporalResource(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_resource.load()

    tenant_id = os.getenv("TENANT_ID", "development")

    transformer = AtlasTransformer(
        connector_name=TemporalConstants.APPLICATION_NAME.value,
        connector_type="sql",
        tenant_id=tenant_id,
    )

    workflow: SQLWorkflow = (
        SampleSQLWorkflowBuilder()
        .set_transformer(transformer)
        .set_temporal_resource(temporal_resource)
        .set_sql_resource(PostgreSQLResource(SQLResourceConfig()))
        .build()
    )

    worker: WorkflowWorker = WorkflowWorker(
        temporal_resource=temporal_resource,
        temporal_activities=workflow.get_activities(),
        workflow_classes=[SQLWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)

    workflow_response = await workflow.start(
        {
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
                "exclude_views": True,
                "exclude_empty_tables": False,
            },
        }
    )

    return workflow_response


async def main():
    await asyncio.gather(start_workflow(), start_fast_api_app())


if __name__ == "__main__":
    asyncio.run(main())
