"""SQL query extraction (mining) example using v3 SqlQueryExtractor template.

Demonstrates how to build a Snowflake query miner by subclassing
SqlQueryExtractor with declarative EntityDef-driven orchestration.

Entity-driven orchestration:
- Declare entities via the ``entities`` class variable
- The "queries" entity handles batching internally
  (get_query_batches → fetch_queries per batch)
- Custom entities dispatch to fetch_{name}() by convention

Usage:
    python examples/application_sql_miner.py
"""

import asyncio
from typing import Any

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
from application_sdk.templates.entity import EntityDef

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

    Declares a single "queries" entity. The base run() orchestrates
    the batch loop: get_query_batches → fetch_queries per batch.
    """

    entities = [
        EntityDef(name="queries", phase=1),
    ]

    @task(timeout_seconds=600)
    async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
        """Determine batch count from Snowflake query history."""
        client = SQLClient(credentials={"account_id": "demo.snowflakecomputing.com"})
        await client.load(credentials=client.credentials)
        rows: list[dict[str, Any]] = []
        async for batch in client.run_query(
            "SELECT COUNT(*) AS total FROM QUERY_HISTORY "
            "WHERE START_TIME >= CURRENT_DATE - INTERVAL '2 WEEK'"
        ):
            rows.extend(batch)
        total_count = rows[0]["total"] if rows else 0
        batch_size = input.workflow_args.get("batch_size", 5000)
        total_batches = max(1, (total_count + batch_size - 1) // batch_size)
        return QueryBatchOutput(
            total_batches=total_batches,
            batch_size=batch_size,
            total_count=total_count,
        )

    @task(timeout_seconds=3600, heartbeat_timeout_seconds=60, auto_heartbeat_seconds=10)
    async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
        """Fetch one batch of queries from Snowflake."""
        client = SQLClient(credentials={"account_id": "demo.snowflakecomputing.com"})
        await client.load(credentials=client.credentials)
        rows: list[dict[str, Any]] = []
        offset = input.batch_number * input.batch_size
        async for batch in client.run_query(
            f"{FETCH_QUERIES_SQL} LIMIT {input.batch_size} OFFSET {offset}"
        ):
            rows.extend(batch)
        return QueryFetchOutput(
            batch_number=input.batch_number,
            queries_fetched=len(rows),
            chunk_count=1,
        )


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
