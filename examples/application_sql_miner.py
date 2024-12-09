"""
This example demonstrates how to create a SQL Miner workflow for extracting query metadata from a Snowflake database.
It uses the Temporal workflow engine to manage the extraction process.

Workflow steps:
1. Perform preflight checks
2. Create an output directory
3. Fetch query information
4. Push results to object store

Usage:
1. Set the Snowflake connection credentials as environment variables
2. Run the script to start the Temporal worker and execute the workflow

Note: This example is specific to Snowflake but can be adapted for other SQL databases.
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from application_sdk.workflows.sql.builders.builder import SQLMinerBuilder
from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.sql.workflows.miner import SQLMinerWorkflow
from application_sdk.workflows.workers.worker import WorkflowWorker

APPLICATION_NAME = "snowflake"

logger = logging.getLogger(__name__)


FETCH_QUERIES_SQL = """
WITH qs AS (
    SELECT * FROM (
        SELECT
            min(start_time) AS SESSION_CREATED_ON,
            SESSION_ID
        FROM
            {database_name_cleaned}.{schema_name_cleaned}.QUERY_HISTORY
        WHERE
            START_TIME >= CURRENT_DATE - INTERVAL '3 WEEK'
            AND START_TIME <= CURRENT_DATE - INTERVAL '1 DAY'
        GROUP BY
            SESSION_ID
        ) ss
        WHERE
            ss.SESSION_CREATED_ON > TO_TIMESTAMP_TZ([START_MARKER], 3)
            AND ss.SESSION_CREATED_ON >= TO_TIMESTAMP_TZ({miner_start_time_epoch})
            AND ss.SESSION_CREATED_ON >= CURRENT_DATE - INTERVAL '30 DAYS'
    ),
    q AS (
        SELECT
            *,
            CASE WHEN warehouse_size = 'X-Small' THEN 1
                 WHEN warehouse_size = 'Small'    THEN 2
                 WHEN warehouse_size = 'Medium'   THEN 4
                 WHEN warehouse_size = 'Large'    THEN 8
                 WHEN warehouse_size = 'X-Large'  THEN 16
                 WHEN warehouse_size = '2X-Large' THEN 32
                 WHEN warehouse_size = '3X-Large' THEN 64
                 WHEN warehouse_size = '4X-Large' THEN 128
                ELSE 1
            END as WAREHOUSE_PRICE
        FROM
            {database_name_cleaned}.{schema_name_cleaned}.QUERY_HISTORY
        WHERE
            EXECUTION_STATUS = 'SUCCESS'
            AND QUERY_TYPE NOT IN
            ('COMMIT', 'USE', 'BEGIN_TRANSACTION', 'DESCRIBE', 'ROLLBACK', 'SHOW', 'ALTER_SESSION', 'GRANT')
            AND START_TIME <= CURRENT_DATE - INTERVAL '1 DAY'
            AND START_TIME >= CURRENT_DATE - INTERVAL '2 WEEK'
    )
    SELECT
        q.* EXCLUDE(START_TIME, END_TIME),
        CONVERT_TIMEZONE('UTC', q.START_TIME) as START_TIME,
        CONVERT_TIMEZONE('UTC', q.END_TIME) as END_TIME,
        q.QUERY_TYPE as SOURCE_QUERY_TYPE,
        to_double(((q.execution_time / (1000 * 3600)) * q.WAREHOUSE_PRICE)) as CREDITS_USED_COMPUTE,
        CONVERT_TIMEZONE('UTC', qs.SESSION_CREATED_ON) as SESSION_CREATED_ON,
        s.CLIENT_VERSION,
        s.CLIENT_BUILD_ID,
        s.CLIENT_ENVIRONMENT,
        s.LOGIN_EVENT_ID,
        s.CLIENT_APPLICATION_ID,
        s.CLIENT_APPLICATION_VERSION,
        s.AUTHENTICATION_METHOD
    FROM
        q inner JOIN qs ON q.SESSION_ID = qs.SESSION_ID LEFT JOIN
        {database_name_cleaned}.{schema_name_cleaned}.SESSIONS s ON q.SESSION_ID = s.SESSION_ID
    ORDER BY
        SESSION_CREATED_ON,
        SESSION_ID,
        START_TIME
"""


class SampleSQLMinerWorkflow(SQLMinerWorkflow):
    fetch_queries_sql = FETCH_QUERIES_SQL


class SampleSQLMinerWorkflowBuilder(SQLMinerBuilder):
    def build(self, miner: SQLMinerWorkflow | None = None) -> SQLMinerWorkflow:
        return super().build(miner=miner or SampleSQLMinerWorkflow())


class SnowflakeSQLResource(SQLResource):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password = quote_plus(self.config.credentials["password"])
        base_url = f"snowflake://{self.config.credentials['user']}:{encoded_password}@{self.config.credentials['account_id']}"

        # FIXME: add more params
        if self.config.credentials.get("warehouse"):
            base_url = f"{base_url}?warehouse={self.config.credentials['warehouse']}"
        if self.config.credentials.get("role"):
            if "?" in base_url:
                base_url = f"{base_url}&role={self.config.credentials['role']}"
            else:
                base_url = f"{base_url}?role={self.config.credentials['role']}"

        return base_url


class SnowflakeResource(SQLResource):
    default_database_alias_key = "database_name"
    default_schema_alias_key = "name"


async def main():
    temporal_resource = TemporalResource(
        TemporalConfig(
            application_name=APPLICATION_NAME,
        )
    )
    await temporal_resource.load()

    sql_resource = SnowflakeSQLResource(SQLResourceConfig())

    miner_workflow: SQLMinerWorkflow = (
        SampleSQLMinerWorkflowBuilder()
        .set_temporal_resource(temporal_resource)
        .set_sql_resource(sql_resource)
        .build()
    )

    worker: WorkflowWorker = WorkflowWorker(
        temporal_resource=temporal_resource,
        temporal_activities=miner_workflow.get_activities(),
        workflow_classes=[SQLMinerWorkflow],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)
    start_time_epoch = int((datetime.now() - timedelta(days=5)).timestamp())

    await miner_workflow.start(
        {
            "miner_args": {
                "database_name_cleaned": "SNOWFLAKE",
                "schema_name_cleaned": "ACCOUNT_USAGE",
                "miner_start_time_epoch": start_time_epoch,
                "chunk_size": 500,
                "current_marker": start_time_epoch,
                "timestamp_column": "START_TIME",
                "sql_replace_from": "ss.SESSION_CREATED_ON > TO_TIMESTAMP_TZ([START_MARKER], 3)",
                "sql_replace_to": "ss.SESSION_CREATED_ON >= TO_TIMESTAMP_TZ([START_MARKER], 3) AND ss.SESSION_CREATED_ON <= TO_TIMESTAMP_TZ([END_MARKER], 3)",
                "ranged_sql_start_key": "[START_MARKER]",
                "ranged_sql_end_key": "[END_MARKER]",
            },
            "credentials": {
                "account_id": os.getenv("SNOWFLAKE_ACCOUNT_ID", "localhost"),
                "user": os.getenv("SNOWFLAKE_USER", "postgres"),
                "password": os.getenv("SNOWFLAKE_PASSWORD", "password"),
                "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", "PHOENIX_TEST"),
                "role": os.getenv("SNOWFLAKE_ROLE", "PHEONIX_APP_TEST"),
            },
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude_filter": "{}",
                "include_filter": '{"^SNOWFLAKE$":["^SECURITY_ESSENTIALS$","^TRUST_CENTER$","^CIS_BENCHMARKS$","^ORGANIZATION_USAGE_LOCAL$","^THREAT_INTELLIGENCE$","^TRUST_CENTER_STATE$","^CIS_COMMON$","^TELEMETRY$","^THREAT_INTELLIGENCE_COMMON$","^LOCAL$","^EXTERNAL_ACCESS$"]}',
                "temp_table_regex": "",
                "advanced_config_strategy": "default",
                "use_source_schema_filtering": "false",
                "use_jdbc_internal_methods": "true",
                "authentication": "BASIC",
                "extraction-method": "direct",
            },
        }
    )

    # wait for the workflow to finish
    time.sleep(120)


if __name__ == "__main__":
    asyncio.run(main())
