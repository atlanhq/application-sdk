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
import time
from datetime import datetime, timedelta
from typing import Any, Dict
from urllib.parse import quote_plus

from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities
from application_sdk.clients.sql import SQLClient
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers.sql import SQLHandler
from application_sdk.worker import Worker
from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow

logger = AtlanLoggerAdapter(logging.getLogger(__name__))

APPLICATION_NAME = "snowflake"


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


class SampleSQLMinerActivities(SQLQueryExtractionActivities):
    fetch_queries_sql = FETCH_QUERIES_SQL


class SnowflakeSQLClient(SQLClient):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password = quote_plus(self.credentials["password"])
        base_url = f"snowflake://{self.credentials['user']}:{encoded_password}@{self.credentials['account_id']}"

        # FIXME: add more params
        if self.credentials.get("warehouse"):
            base_url = f"{base_url}?warehouse={self.credentials['warehouse']}"
        if self.credentials.get("role"):
            if "?" in base_url:
                base_url = f"{base_url}&role={self.credentials['role']}"
            else:
                base_url = f"{base_url}?role={self.credentials['role']}"

        return base_url


class SampleSnowflakeHandler(SQLHandler):
    tables_check_sql = """
        SELECT count(*) as "count"
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
            WHERE NOT TABLE_NAME RLIKE '{exclude_table}'
                AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_exclude_regex}'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_include_regex}';
        """
    metadata_sql = "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA;"


async def application_sql_miner(daemon: bool = True) -> Dict[str, Any]:
    print("Starting application_sql_miner")

    temporal_client = TemporalClient(
        application_name=APPLICATION_NAME,
    )
    await temporal_client.load()

    activities = SampleSQLMinerActivities(
        sql_client_class=SnowflakeSQLClient, handler_class=SampleSnowflakeHandler
    )

    worker: Worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[SQLQueryExtractionWorkflow],
        temporal_activities=SQLQueryExtractionWorkflow.get_activities(activities),
    )

    # wait for the worker to start
    time.sleep(3)
    start_time_epoch = int((datetime.now() - timedelta(days=2)).timestamp())

    workflow_args = {
        "miner_args": {
            "database_name_cleaned": "SNOWFLAKE",
            "schema_name_cleaned": "ACCOUNT_USAGE",
            "miner_start_time_epoch": start_time_epoch,
            "chunk_size": 5000,
            "current_marker": start_time_epoch,
            "timestamp_column": "START_TIME",
            "sql_replace_from": "ss.SESSION_CREATED_ON > TO_TIMESTAMP_TZ([START_MARKER], 3)",
            "sql_replace_to": "ss.SESSION_CREATED_ON >= TO_TIMESTAMP_TZ([START_MARKER], 3) AND ss.SESSION_CREATED_ON <= TO_TIMESTAMP_TZ([END_MARKER], 3)",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        },
        "credentials": {
            "account_id": os.getenv("SNOWFLAKE_ACCOUNT_ID", "localhost"),
            "user": os.getenv("SNOWFLAKE_USER", "snowflake"),
            "password": os.getenv("SNOWFLAKE_PASSWORD", "password"),
            "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", "PHOENIX_TEST"),
            "role": os.getenv("SNOWFLAKE_ROLE", "PHEONIX_APP_TEST"),
        },
        "connection": {
            "connection_name": "test-connection",
            "connection_qualified_name": "default/postgres/1728518400",
        },
        "metadata": {
            "exclude_filter": "{}",
            "include_filter": '{"^E2E_TEST_DB$":["^HIERARCHY_OFFER75$"]}',
            "temp_table_regex": "",
            "advanced_config_strategy": "default",
            "use_source_schema_filtering": "false",
            "use_jdbc_internal_methods": "true",
            "authentication": "BASIC",
            "extraction-method": "direct",
        },
    }

    workflow_response = await temporal_client.start_workflow(
        workflow_class=SQLQueryExtractionWorkflow,
        workflow_args=workflow_args,
    )

    await worker.start(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_sql_miner(daemon=False))
