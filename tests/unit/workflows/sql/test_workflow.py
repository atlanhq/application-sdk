import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.clients.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from application_sdk.clients.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers import TransformerInterface


@pytest.fixture
def sql_resource():
    resource = SQLResource(SQLResourceConfig())
    resource.run_query = AsyncMock()
    resource.sql_input = MagicMock()
    return resource


@pytest.fixture
def temporal_resource():
    return TemporalResource(TemporalConfig(application_name="test-app"))


@pytest.fixture
def transformer():
    mock_transformer = MagicMock(spec=TransformerInterface)
    mock_transformer.transform_metadata.return_value = {"transformed": True}
    return mock_transformer


@pytest.fixture
def workflow(sql_resource, temporal_resource, transformer):
    workflow = SQLWorkflow()
    workflow.set_sql_resource(sql_resource)
    workflow.set_temporal_resource(temporal_resource)
    workflow.set_transformer(transformer)
    return workflow


def test_workflow_initialization():
    workflow = SQLWorkflow()
    assert workflow.application_name == "sql-connector"
    assert workflow.batch_size == 100000
    assert workflow.max_transform_concurrency == 5
    assert workflow.sql_resource is None
    assert workflow.transformer is None


def test_workflow_setters(sql_resource, temporal_resource, transformer):
    workflow = SQLWorkflow()

    # Test setting SQL resource
    workflow.set_sql_resource(sql_resource)
    assert workflow.sql_resource == sql_resource

    # Test setting temporal resource
    workflow.set_temporal_resource(temporal_resource)
    assert workflow.temporal_resource == temporal_resource

    # Test setting transformer
    workflow.set_transformer(transformer)
    assert workflow.transformer == transformer

    # Test setting application name
    workflow.set_application_name("test-app")
    assert workflow.application_name == "test-app"

    # Test setting batch size
    workflow.set_batch_size(5000)
    assert workflow.batch_size == 5000

    # Test setting max transform concurrency
    workflow.set_max_transform_concurrency(3)
    assert workflow.max_transform_concurrency == 3


@pytest.mark.asyncio
async def test_start_without_sql_resource():
    workflow = SQLWorkflow()
    with pytest.raises(ValueError, match="SQL resource is not set"):
        await workflow.start({"credentials": {}})


@pytest.mark.asyncio
async def test_transform_batch_without_transformer(sql_resource):
    workflow = SQLWorkflow()
    workflow.set_sql_resource(sql_resource)

    with pytest.raises(ValueError, match="Transformer is not set"):
        await workflow._transform_batch(
            [{"test": "data"}],
            "test",
            workflow_id="test-workflow",
            workflow_run_id="test-run",
        )


def normalize_sql(query: str) -> str:
    """
    Normalize SQL queries by removing extra whitespace, line breaks, and indentation.
    """
    return re.sub(r"\s+", " ", query).strip()


@pytest.mark.asyncio
async def test_prepare_query():
    test_cases = [
        {
            "query": """SELECT
                        S.COMMENT AS REMARKS, S.*, IFNULL(T.TABLE_COUNT, 0) AS TABLE_COUNT, IFNULL(V.VIEW_COUNT, 0) AS VIEW_COUNT
                    FROM
                        SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA s
                            LEFT JOIN (
                            SELECT TABLE_SCHEMA_ID, COUNT(*) AS TABLE_COUNT FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES  WHERE TABLE_TYPE LIKE '%TABLE%' AND DELETED IS NULL GROUP BY TABLE_SCHEMA_ID
                            ) AS T ON S.SCHEMA_ID = T.TABLE_SCHEMA_ID
                            LEFT JOIN (
                            SELECT TABLE_SCHEMA_ID, COUNT(*) AS VIEW_COUNT FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES WHERE TABLE_TYPE LIKE '%VIEW%' AND DELETED IS NULL GROUP BY TABLE_SCHEMA_ID
                            ) AS V ON S.SCHEMA_ID = V.TABLE_SCHEMA_ID
                    WHERE
                        deleted IS NULL
                        and concat(CATALOG_NAME, concat('.', SCHEMA_NAME)) NOT REGEXP '{normalized_exclude_regex}'
                        and concat(CATALOG_NAME, concat('.', SCHEMA_NAME)) REGEXP '{normalized_include_regex}';""",
            "workflow_args": {
                "metadata": {"include_filter": "{}", "exclude_filter": "{}"}
            },
            "expected": """SELECT
                            S.COMMENT AS REMARKS, S.*, IFNULL(T.TABLE_COUNT, 0) AS TABLE_COUNT, IFNULL(V.VIEW_COUNT, 0) AS VIEW_COUNT
                        FROM
                            SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA s
                                LEFT JOIN (
                                SELECT TABLE_SCHEMA_ID, COUNT(*) AS TABLE_COUNT FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES  WHERE TABLE_TYPE LIKE '%TABLE%' AND DELETED IS NULL GROUP BY TABLE_SCHEMA_ID
                                ) AS T ON S.SCHEMA_ID = T.TABLE_SCHEMA_ID
                                LEFT JOIN (
                                SELECT TABLE_SCHEMA_ID, COUNT(*) AS VIEW_COUNT FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES WHERE TABLE_TYPE LIKE '%VIEW%' AND DELETED IS NULL GROUP BY TABLE_SCHEMA_ID
                                ) AS V ON S.SCHEMA_ID = V.TABLE_SCHEMA_ID
                        WHERE
                            deleted IS NULL
                            and concat(CATALOG_NAME, concat('.', SCHEMA_NAME)) NOT REGEXP '^$'
                            and concat(CATALOG_NAME, concat('.', SCHEMA_NAME)) REGEXP '.*';""",
        },
    ]

    for case in test_cases:
        result = SQLWorkflow.prepare_query(case["query"], case["workflow_args"])
        # Normalize both the result and the expected SQL before asserting
        assert normalize_sql(result) == normalize_sql(case["expected"])
