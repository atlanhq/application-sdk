import asyncio
import os
import threading
import time
from typing import Any, Dict, List
from urllib.parse import quote_plus

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
from application_sdk.workflows.sql.builders.builder import SQLWorkflowBuilder
from application_sdk.workflows.sql.resources.async_sql_resource import AsyncSQLResource
from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers.atlas import AtlasTransformer
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
    # @activity.defn
    # async def activity_1(self, workflow_args: Any):
    #     print("Activity 1")
    #     pass

    # @activity.defn
    # async def activity_2(self, workflow_args: Any):
    #     print("Activity 2")
    #     pass

    @workflow.run
    async def run(self, workflow_args: Any):
        pass

    async def start(self, workflow_args: Any, workflow_class: Any):
        return await super().start(workflow_args, self.__class__)


class SampleWorkflowBuilder(WorkflowBuilderInterface):
    temporal_resource: TemporalResource

    def build(self, workflow: SampleWorkflow | None = None) -> SampleWorkflow:
        return SampleWorkflow().set_temporal_resource(self.temporal_resource)


class SampleFastAPIApplication(FastAPIApplication):
    async def on_activity_start(self, event: dict[str, Any]):
        print(f"EVENT: Activity start: {event}")
        return

    async def on_activity_end(self, event: dict[str, Any]):
        print(f"EVENT: Activity end: {event}")
        return

    async def on_workflow_start(self, event: dict[str, Any]):
        print(f"EVENT: Workflow start: {event}")
        return

    async def on_workflow_end(self, event: dict[str, Any]):
        print(f"EVENT: Workflow end: {event}")
        return

    async def on_custom_event(self, event: dict[str, Any]):
        print(f"EVENT: Custom event: {event}")
        return


async def start_fast_api_app():
    fast_api_app = SampleFastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SQLWorkflow(),
    )
    fast_api_app.subscribe_to_workflow_end(
        app_name=TemporalConstants.APPLICATION_NAME.value
    )
    fast_api_app.subscribe_to_workflow_start(
        app_name=TemporalConstants.APPLICATION_NAME.value
    )

    await fast_api_app.start()


# TEMP
class PostgreSQLResource(AsyncSQLResource):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password: str = quote_plus(self.config.credentials["password"])
        return f"postgresql+psycopg://{self.config.credentials['user']}:{encoded_password}@{self.config.credentials['host']}:{self.config.credentials['port']}/{self.config.credentials['database']}"


# TEMP
@workflow.defn
class SampleSQLWorkflow(SQLWorkflow):
    @workflow.run
    async def run(self, workflow_args: Any):
        print("RUN")
        return


# TEMP
class SampleSQLWorkflowBuilder(SQLWorkflowBuilder):
    def build(self, workflow: SQLWorkflow | None = None) -> SQLWorkflow:
        return super().build(workflow=workflow or SampleSQLWorkflow())


async def start_workflow():
    # await asyncio.sleep(10)

    temporal_resource = TemporalResource(
        TemporalConfig(
            application_name=TemporalConstants.APPLICATION_NAME.value,
        )
    )
    await temporal_resource.load()

    sql_resource = PostgreSQLResource(SQLResourceConfig())

    transformer = AtlasTransformer(
        connector_name=TemporalConstants.APPLICATION_NAME.value,
        connector_type="sql",
        tenant_id="1",
    )

    # workflow: SampleSQLWorkflow = (
    #     SampleSQLWorkflowBuilder()
    #     .set_transformer(transformer)
    #     .set_temporal_resource(temporal_resource)
    #     .set_sql_resource(sql_resource)
    #     .build()
    # )

    workflow: SampleWorkflow = (
        SampleWorkflowBuilder().set_temporal_resource(temporal_resource).build()
    )
    print("hello world222111", WorkflowWorker)

    worker: WorkflowWorker = WorkflowWorker(
        temporal_resource=temporal_resource,
        temporal_activities=workflow.get_activities(),
        workflow_classes=[SampleSQLWorkflow, SampleWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas", "grpc", "dapr"],
    )
    print("hello world", WorkflowWorker)

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)

    # workflow_response = await workflow.start(
    #     {
    #         "credentials": {
    #             "host": os.getenv("POSTGRES_HOST", "localhost"),
    #             "port": os.getenv("POSTGRES_PORT", "5432"),
    #             "user": os.getenv("POSTGRES_USER", "postgres"),
    #             "password": os.getenv("POSTGRES_PASSWORD", "password"),
    #             "database": os.getenv("POSTGRES_DATABASE", "postgres"),
    #         },
    #         "connection": {"connection": "dev"},
    #         "metadata": {
    #             "exclude_filter": "{}",
    #             "include_filter": "{}",
    #             "temp_table_regex": "",
    #             "advanced_config_strategy": "default",
    #             "use_source_schema_filtering": "false",
    #             "use_jdbc_internal_methods": "true",
    #             "authentication": "BASIC",
    #             "extraction-method": "direct",
    #             "exclude_views": True,
    #             "exclude_empty_tables": False,
    #         },
    #     }
    # )

    return await workflow.start({}, SampleWorkflow)


async def main():
    print("hello world 222")
    await asyncio.gather(start_workflow(), start_fast_api_app())


if __name__ == "__main__":
    asyncio.run(main())
