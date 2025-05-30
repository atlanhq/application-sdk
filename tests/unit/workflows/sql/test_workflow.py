import re
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest
from temporalio.common import RetryPolicy

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)
from application_sdk.common.utils import prepare_query
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionWorkflow,
)


@pytest.fixture
def workflow():
    workflow = BaseSQLMetadataExtractionWorkflow()
    return workflow


def test_workflow_initialization():
    workflow = BaseSQLMetadataExtractionWorkflow()
    assert workflow.application_name == "default"
    assert workflow.activities_cls == BaseSQLMetadataExtractionActivities


def test_get_activities():
    """Test get_activities returns correct sequence of activities"""
    workflow = BaseSQLMetadataExtractionWorkflow()
    activities = Mock(spec=BaseSQLMetadataExtractionActivities)

    activity_sequence = workflow.get_activities(activities)

    assert len(activity_sequence) == 8
    assert activity_sequence == [
        activities.preflight_check,
        activities.get_workflow_args,
        activities.fetch_databases,
        activities.fetch_schemas,
        activities.fetch_tables,
        activities.fetch_columns,
        activities.fetch_procedures,
        activities.transform_data,
    ]


def test_get_transform_batches():
    """Test get_transform_batches with different scenarios"""
    workflow = BaseSQLMetadataExtractionWorkflow()
    test_cases = [
        {
            "chunk_count": 10,
            "typename": "test",
            "expected_batch_count": 10,  # One batch per chunk
            "expected_total_files": 10,
            "description": "Multiple chunks",
        },
        {
            "chunk_count": 3,
            "typename": "test",
            "expected_batch_count": 3,
            "expected_total_files": 3,
            "description": "Few chunks",
        },
        {
            "chunk_count": 1,
            "typename": "test",
            "expected_batch_count": 1,
            "expected_total_files": 1,
            "description": "Single chunk",
        },
    ]

    for case in test_cases:
        batches, chunk_starts = workflow.get_transform_batches(
            int(case["chunk_count"]), str(case["typename"])
        )

        # Verify number of batches
        assert len(batches) == case["chunk_count"], case["description"]
        assert len(chunk_starts) == case["chunk_count"], case["description"]

        # Verify total number of files
        total_files = sum(len(batch) for batch in batches)
        assert total_files == case["expected_total_files"], case["description"]

        # Verify file naming format and batch size
        for i, batch in enumerate(batches):
            assert (
                len(batch) == 1
            ), f"Each batch should contain exactly one file: {case['description']}"
            file = batch[0]
            assert file.startswith(f"{case['typename']}/")
            assert file.endswith(".json")
            assert file == f"{case['typename']}/{i+1}.json"

        # Verify chunk start numbers are sequential
        assert chunk_starts == list(
            range(int(case["chunk_count"]))
        ), f"Chunk starts should be sequential: {case['description']}"


@pytest.mark.asyncio
async def test_fetch_and_transform():
    """Test fetch_and_transform method"""
    workflow = BaseSQLMetadataExtractionWorkflow()

    # Mock fetch function
    mock_fetch = AsyncMock()
    mock_fetch.return_value = ActivityStatistics(
        total_record_count=10, chunk_count=2, typename="test"
    ).model_dump()

    # Mock transform function
    mock_transform = AsyncMock()
    mock_transform.return_value = ActivityStatistics(
        total_record_count=5, chunk_count=1, typename="test"
    ).model_dump()

    workflow.activities_cls.transform_data = mock_transform

    workflow_args = {"test": "args"}
    retry_policy = RetryPolicy(maximum_attempts=1)

    with patch("temporalio.workflow.execute_activity_method") as mock_execute:
        mock_execute.side_effect = [mock_fetch.return_value] + [
            mock_transform.return_value
        ] * 2
        await workflow.fetch_and_transform(mock_fetch, workflow_args, retry_policy)

        # Verify fetch was called
        assert mock_execute.call_count == 3


@pytest.mark.asyncio
async def test_fetch_and_transform_error_handling():
    """Test fetch_and_transform error handling"""
    workflow = BaseSQLMetadataExtractionWorkflow()

    # Test with None result
    mock_fetch_none = AsyncMock(return_value=None)
    with patch("temporalio.workflow.execute_activity_method") as mock_execute:
        mock_execute.return_value = ActivityStatistics(
            total_record_count=0, chunk_count=0, typename="test"
        ).model_dump()
        await workflow.fetch_and_transform(
            mock_fetch_none, {}, RetryPolicy(maximum_attempts=1)
        )

    # Test with invalid typename
    mock_fetch_invalid = AsyncMock(
        return_value=ActivityStatistics(
            total_record_count=10, chunk_count=2, typename=None
        ).model_dump()
    )

    with patch("temporalio.workflow.execute_activity_method") as mock_execute:
        mock_execute.return_value = mock_fetch_invalid.return_value
        with pytest.raises(ValueError, match="Invalid typename"):
            await workflow.fetch_and_transform(
                mock_fetch_invalid, {}, RetryPolicy(maximum_attempts=1)
            )


def normalize_sql(query: str | None) -> str:
    """
    Normalize SQL queries by removing extra whitespace, line breaks, and indentation.
    Also normalizes spacing around semicolons and parentheses.
    """
    if query is None:
        return ""
    # First remove all whitespace around semicolons and parentheses
    query = re.sub(r"\s*([;()])\s*", r"\1", query)
    # Then normalize all other whitespace
    return re.sub(r"\s+", " ", query).strip()


@pytest.mark.asyncio
async def test_prepare_query():
    test_cases: List[Dict[str, Any]] = [
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
        {
            "query": """SELECT count(*) as "count"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
                    WHERE NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_exclude_regex}'
                        AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_include_regex}'
                        {temp_table_regex_sql};""",
            "workflow_args": {
                "metadata": {
                    "include-filter": "{}",
                    "exclude-filter": "{}",
                    "temp-table-regex": "",
                }
            },
            "temp_table_regex_sql": "AND NOT TABLE_NAME RLIKE '{exclude_table_regex}'",
            "expected": """SELECT count(*) as "count"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
                    WHERE NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                        AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '.*';""",
        },
    ]

    for case in test_cases:
        result = prepare_query(
            query=case["query"],
            workflow_args=case["workflow_args"],
            temp_table_regex_sql=case.get("temp_table_regex_sql", ""),
        )
        # Normalize both the result and the expected SQL before asserting
        assert normalize_sql(result) == normalize_sql(case["expected"])
