"""SQL query extraction (mining) example using v3 SqlQueryExtractor template.

Demonstrates how to build a Snowflake query miner by subclassing
SqlQueryExtractor. In v3, the workflow + activities split collapses into
a single App class with typed contracts.

Extraction steps (orchestrated by SqlQueryExtractor.run()):
1. Determine query batches (get_query_batches)
2. Fetch queries for each batch (fetch_queries)
3. Aggregate and upload results

Usage:
    python examples/application_sql_miner.py
"""

import asyncio

from application_sdk.app import task
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.main import run_dev_combined
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates import SqlQueryExtractor
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput,
    QueryBatchOutput,
    QueryFetchInput,
    QueryFetchOutput,
)

logger = get_logger(__name__)


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


class SQLClient(BaseSQLClient):
    """Snowflake connection configuration."""

    DB_CONFIG = DatabaseConfig(
        template="snowflake://{username}:{password}@{account_id}",
        required=["username", "password", "account_id"],
        parameters=["warehouse", "role"],
    )


class SnowflakeQueryExtractor(SqlQueryExtractor):
    """Snowflake query miner.

    Overrides get_query_batches and fetch_queries to implement
    Snowflake-specific query history extraction.
    """

    fetch_queries_sql = FETCH_QUERIES_SQL

    @task(timeout_seconds=600)
    async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
        """Determine batch count from Snowflake query history."""
        return QueryBatchOutput(total_batches=0, batch_size=1000, total_count=0)

    @task(timeout_seconds=3600, heartbeat_timeout_seconds=60, auto_heartbeat_seconds=10)
    async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
        """Fetch one batch of queries from Snowflake."""
        return QueryFetchOutput(queries_fetched=0)


class SampleSnowflakeHandler(Handler):
    """Handler for the Snowflake query miner."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(
                    name="queryHistoryCheck",
                    passed=True,
                    message="Query history access verified",
                ),
            ],
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[])


if __name__ == "__main__":
    asyncio.run(
        run_dev_combined(
            SnowflakeQueryExtractor,
            example_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/snowflake/1728518400",
                },
                "credential_guid": "your-credential-guid",
                "lookback_days": 30,
                "batch_size": 5000,
            },
        )
    )
