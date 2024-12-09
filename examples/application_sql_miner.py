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
                "include_filter": '{"^E2E_TEST_DB$":["^HIERARCHY_SHELL5$","^ALTR_DSAAS$","^SCHEMA_TABBY88$","^SCHEMA_LIVER13$","^SCHEMA_PUPIL67$","^SCHEMA_MACAW34$","^SCHEMA_RAZOR24$","^LINEAGE_RULER60$","^LINEAGE_VENTI86$","^LINEAGE_FOCUS80$","^SCHEMA_DRAIN60$","^HIERARCHY_BREAK21$","^SCHEMA_LEASH8$","^SCHEMA_CHECK33$","^HIERARCHY_STEAM15$","^SCHEMA_FLECK31$","^SALESFORCE_REDHAT$","^LINEAGE_OFFER75$","^LINEAGE_METER2$","^LINEAGE_STEAM15$","^SCHEMA_OCTET21$","^HIERARCHY_BEACH46$","^LINEAGE_FLAIR75$","^HIERARCHY_SCORN47$","^SCHEMA_TREND88$","^LINEAGE_BEACH46$","^SCHEMA_COMMA66$","^HIERARCHY_MEANS72$","^TAGS_SCHEMA$","^SCHEMA_WORRY95$","^SCHEMA_CATCH79$","^SCHEMA_MOLAR78$","^BRONZE_APPLICATION$","^PROCESSED_GOLD$","^LINEAGE_DRAKE5$","^LINEAGE_FLICK67$","^SCHEMA_DRILL78$","^HIERARCHY_METER2$","^LINEAGE_ROUND25$","^LINEAGE_SHELL5$","^SCHEMA_DRAIN89$","^HIERARCHY_RULER60$","^SCHEMA_OCEAN55$","^SCHEMA_PAGAN93$","^SCHEMA_ONSET65$","^HIERARCHY_SENSE96$","^HIERARCHY_FLAIR69$","^HIERARCHY_STORM18$","^SCHEMA_FAULT34$","^HIERARCHY_LEASH21$","^HIERARCHY_HORSE62$","^SCHEMA_GRASP26$","^SCHEMA_GAUGE26$","^HIERARCHY_BASIS18$","^SCHEMA_FLICK90$","^SCHEMA_STYLE79$","^SCHEMA_GOOSE76$","^SCHEMA_HUMOR68$","^SCHEMA_SHAWL11$","^PUBLIC$","^LINEAGE_LEASH21$","^LINEAGE_BREAK21$","^SCHEMA_DRINK83$","^SCHEMA_SNUCK63$","^HIERARCHY_DRAKE5$","^HIERARCHY_OFFER75$","^SCHEMA_MOOSE51$","^HIERARCHY_FOCUS80$","^SCHEMA_STAKE72$","^SCHEMA_GOOSE20$","^HIERARCHY_ALIAS75$","^SCHEMA_SHEEP73$","^SCHEMA_NOUN_79$","^LINEAGE_FLAIR69$","^SCHEMA_ELVER40$","^SCHEMA_PANDA68$","^LINEAGE_ALIAS75$","^BRONZE_SALES$","^SCHEMA_PRIDE87$","^SCHEMA_INFIX88$","^SCHEMA_LOVER55$","^SCHEMA_JEANS24$","^SCHEMA_SHELF28$","^SCHEMA_POUCH92$","^SCHEMA_BRASS50$","^SCHEMA_IRONY12$","^SCHEMA_CURVE_71$","^HIERARCHY_BLADE80$","^SCHEMA_CREST5$","^HIERARCHY_ROUND25$","^SCHEMA_DRAKE41$","^FIVETRAN_PLANTING_LIKEWISE_STAGING$","^SCHEMA_SPAWN31$","^SCHEMA_GOING95$","^SCHEMA_CHURN48$","^LINEAGE_MEANS72$","^SCHEMA_ANIME7$","^HIERARCHY_CELLO96$","^SCHEMA_ROUND94$","^SCHEMA_DIVAN81$","^SCHEMA_BOXER44$","^SCHEMA_ALIAS3$","^SCHEMA_MIXER51$","^LINEAGE_HERON93$","^SCHEMA_MOUND75$","^SCHEMA_AGLET88$","^SCHEMA_COCOA15$","^SCHEMA_KENDO76$","^SCHEMA_HUTCH95$","^SCHEMA_SALSA12$","^SCHEMA_TABLE_1$","^PROCESSED_SILVER$","^HIERARCHY_VENTI86$","^SCHEMA_GRILL12$","^LINEAGE_PARKA39$","^SCHEMA_MIXER96$","^LINEAGE_STORM18$","^HIERARCHY_HERON93$","^LINEAGE_SNARL62$","^SCHEMA_SWINE61$","^SCHEMA_LOVER81$","^SCHEMA_DRINK_28$","^SCHEMA_GLORY32$","^SCHEMA_BRASS57$","^HIERARCHY_FLAIR75$","^HIERARCHY_SNARL62$","^LINEAGE_BASIS18$","^HIERARCHY_FLICK67$","^SCHEMA_WRONG12$","^SCHEMA_HEART63$","^SCHEMA_DEVIL28$","^BRONZE_WAREHOUSE$","^LINEAGE_BLADE80$","^LINEAGE_SCORN47$","^LINEAGE_CELLO96$","^SCHEMA_CREST10$","^SCHEMA_SLIME91$","^LINEAGE_SENSE96$","^LINEAGE_HORSE62$","^SCHEMA_WORLD11$","^SCHEMA_TRIAD83$","^HIERARCHY_PARKA39$"]}',
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
