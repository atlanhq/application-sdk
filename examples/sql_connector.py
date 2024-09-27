"""
This examples demonstrates how to connect to a SQL database and extract metadata from it.

Usage Instructions:
- Set the usage credentials.

Actions performed by this workflow:
- Perform preflight checks
- Spawns a temporal workflow to execute the following activities
- Workflow Activities
    - Create an output directory
    - Fetch databases
    - Fetch schemas
    - Clean up output directory
    - Push results to object store
"""

# from datetime import time
from datetime import timedelta
import os
import shutil
import time
import threading
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

import asyncio
from typing import Any, Coroutine, Dict, List
from urllib.parse import quote_plus
from application_sdk.dto.workflow import WorkflowConfig
from application_sdk.interfaces.platform import Platform
from application_sdk.workflows.sql import SQLWorkflowBuilderInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import SQLWorkflowPreflightCheckInterface
from application_sdk.workflows.sql.utils import fetch_and_process_data, prepare_filters
from application_sdk.workflows.sql.workflow import SQLWorkflowWorkerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater

APPLICATION_NAME = "sample-sql-workflow"

class SampleSQLWorkflowMetadata(SQLWorkflowMetadataInterface):
    METADATA_SQL = """
    SELECT schema_name, catalog_name
    FROM INFORMATION_SCHEMA.SCHEMATA;
    """

class SampleSQLWorkflowPreflight(SQLWorkflowPreflightCheckInterface):
    METADATA_SQL = """
    SELECT schema_name, catalog_name
    FROM INFORMATION_SCHEMA.SCHEMATA;
    """
    TABLES_CHECK_SQL = """
    SELECT count(*)
    FROM INFORMATION_SCHEMA.TABLES;
    """

class SQLWorkflowActivities:
    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def setup_output_directory(output_prefix: str) -> None:
        os.makedirs(output_prefix, exist_ok=True)
        os.makedirs(os.path.join(output_prefix, "raw"), exist_ok=True)
        os.makedirs(os.path.join(output_prefix, "transformed"), exist_ok=True)

        activity.logger.info(f"Created output directory: {output_prefix}")

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_databases(config: WorkflowConfig):
        # config = WorkflowConfig(**workflow_args["config"])
        credentials = Platform.extract_credentials(config.credentialsGUID)
        sql_query = """
        SELECT * FROM pg_database WHERE datname = current_database();
        """
        return await fetch_and_process_data(credentials, config.outputPath, sql_query, "database")

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_schemas(config: WorkflowConfig):
        credentials = Platform.extract_credentials(config.credentialsGUID)

        normalized_include_regex, normalized_exclude_regex, exclude_table = (
            prepare_filters(
                config.includeFilterStr,
                config.excludeFilterStr,
                config.tempTableRegexStr,
            )
        )
        
        SCHEMA_SQL = """
        SELECT * FROM INFORMATION_SCHEMA.SCHEMATA s
        WHERE
            s.schema_name NOT LIKE 'pg_%'
            AND s.schema_name != 'information_schema'
            AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}'
            AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';
        """

        schema_sql_query = SCHEMA_SQL.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
        )
        return await fetch_and_process_data(credentials, config.outputPath, schema_sql_query, "schema")


    @staticmethod
    @activity.defn
    async def teardown_output_directory(output_prefix: str) -> None:
        activity.logger.info(f"Tearing down output directory: {output_prefix}")
        shutil.rmtree(output_prefix)

    @staticmethod
    @activity.defn
    async def push_results_to_object_store(output_config: Dict[str, str]) -> None:
        activity.logger.info("Pushing results to object store")
        try:
            output_prefix, output_path = (
                output_config["output_prefix"],
                output_config["output_path"],
            )
            Platform.push_to_object_store(output_prefix, output_path)
        except Exception as e:
            activity.logger.error(f"Error pushing results to object store: {e}")
            raise e


@workflow.defn
class SQLWorkflowDefinition:
    @workflow.run
    async def run(self, config: WorkflowConfig) -> Dict[str, Any]:
        workflow.logger.info(f"Starting extraction workflow for {config.workflowId}")
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        workflow_run_id = workflow.info().run_id
        config.outputPath = (
            f"{config.outputPrefix}/{config.workflowId}/{workflow_run_id}"
        )

        # Create output directory
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            SQLWorkflowActivities.setup_output_directory,
            config.outputPath,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=5),
        )

        # run activities in parallel
        activities: List[Coroutine[Any, Any, Any]] = [
            workflow.execute_activity(
                SQLWorkflowActivities.fetch_databases,
                config,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
            workflow.execute_activity(
                SQLWorkflowActivities.fetch_schemas,
                config,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
        ]

        # Wait for all activities to complete
        results = await asyncio.gather(*activities)
        extraction_results: Dict[str, Any] = {}
        for result in results:
            extraction_results.update(result)
        
        # Push results to object store
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            SQLWorkflowActivities.push_results_to_object_store,
            {"output_prefix": config.outputPrefix, "output_path": config.outputPath},
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(minutes=10),
        )

        # cleanup output directory
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            SQLWorkflowActivities.teardown_output_directory,
            config.outputPath,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=5),
        )
        workflow.logger.info(f"Extraction workflow completed for {config.workflowId}")
        workflow.logger.info(f"Extraction results summary: {extraction_results}")


class SampleSQLWorkflowWorker(SQLWorkflowWorkerInterface):
    TEMPORAL_WORKFLOW_NAME = SQLWorkflowDefinition
    TEMPORAL_ACTIVITIES = [
        SQLWorkflowActivities.setup_output_directory,
        SQLWorkflowActivities.fetch_databases,
        SQLWorkflowActivities.fetch_schemas,
        SQLWorkflowActivities.teardown_output_directory,
        SQLWorkflowActivities.push_results_to_object_store,
    ]
    PASSTHROUGH_MODULES = ["application_sdk"]


class SampleSQLWorkflowBuilder(SQLWorkflowBuilderInterface):
    def get_sqlalchemy_connection_string(self, credentials: Dict[str, Any]) -> str:
        encoded_password = quote_plus(credentials["password"])
        return f"postgresql+psycopg2://{credentials['user']}:{encoded_password}@{credentials['host']}:{credentials['port']}/{credentials['database']}"

    def get_sqlalchemy_connect_args(
        self, credentials: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {}

    def __init__(self):
        # Preflight check
        preflight_check = SampleSQLWorkflowPreflight(
            self.get_sqlalchemy_connection_string,
            self.get_sqlalchemy_connect_args,
        )

        # Metadata interface
        metadata_interface = SampleSQLWorkflowMetadata(
            self.get_sqlalchemy_connection_string,
            self.get_sqlalchemy_connect_args,
        )

        # Worker interface
        worker_interface = SampleSQLWorkflowWorker(
            APPLICATION_NAME,
        )

        super().__init__(
            metadata_interface=metadata_interface,
            preflight_check_interface=preflight_check,
            worker_interface=worker_interface,
        )

def run_worker(worker_interface):
    asyncio.run(worker_interface.start_worker())


if __name__ == "__main__":
    builder = SampleSQLWorkflowBuilder()
    # Start the temporal worker in a separate thread
    worker_thread = threading.Thread(
        target=run_worker,
        args=(builder.worker_interface,),
        daemon=True
    )
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)
    
    # submit workflow
    asyncio.run(builder.worker_interface.workflow_execution_handler(
        {
            "credentials": {
                "host": "host",
                "port": 5432,
                "user": "postgres",
                "password": "password",
                "database": "postgres"
            },
            "connection": {
                "connection": "dev"
            },
            "metadata": {
                "exclude-filter": "{}",
                "include-filter": "{}",
                "temp-table-regex": "",
                "advanced-config-strategy": "default",
                "use-source-schema-filtering": "false",
                "use-jdbc-internal-methods": "true",
                "authentication": "BASIC",
                "extraction-method": "direct"
            }
        }
    ))

    # wait for the workflow to finish
    time.sleep(30)
