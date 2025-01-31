import re

import pytest

from application_sdk.common.utils import prepare_query
from application_sdk.workflows.metadata_extraction.sql import (
    SQLMetadataExtractionWorkflow,
)


@pytest.fixture
def workflow():
    workflow = SQLMetadataExtractionWorkflow()
    return workflow


def test_workflow_initialization():
    workflow = SQLMetadataExtractionWorkflow()
    assert workflow.application_name == "default"
    assert workflow.batch_size == 100000
    assert workflow.max_transform_concurrency == 5


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
                "metadata": {"include-filter": "{}", "exclude-filter": "{}"}
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
        result = prepare_query(case["query"], case["workflow_args"])
        # Normalize both the result and the expected SQL before asserting
        assert normalize_sql(result) == normalize_sql(case["expected"])
