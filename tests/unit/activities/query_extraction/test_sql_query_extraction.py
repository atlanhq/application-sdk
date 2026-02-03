from typing import Any, Dict

import pytest
from pydantic import ValidationError

from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities

BASE_QUERY = (
    "SELECT * FROM {database_name_cleaned}.{schema_name_cleaned} "
    "WHERE {timestamp_column} >= {miner_start_time_epoch} AND {sql_replace_from}"
)


@pytest.fixture()
def activities() -> SQLQueryExtractionActivities:
    """Create a SQLQueryExtractionActivities instance for tests."""
    return SQLQueryExtractionActivities()


@pytest.fixture()
def base_workflow_args() -> Dict[str, Any]:
    """Provide a baseline workflow_args payload for formatting tests."""
    return {
        "miner_args": {
            "database_name_cleaned": "SNOWFLAKE",
            "schema_name_cleaned": "ACCOUNT_USAGE",
            "timestamp_column": "START_TIME",
            "chunk_size": 100,
            "current_marker": 0,
            "sql_replace_from": "SQL_RANGE_CLAUSE",
            "sql_replace_to": "START_TIME >= [START_MARKER] AND START_TIME <= [END_MARKER]",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
            "miner_start_time_epoch": 1700000000,
        },
        "start_marker": "1700000001",
        "end_marker": "1700000100",
    }


def test_get_formatted_query_formats_valid_inputs(
    activities: SQLQueryExtractionActivities, base_workflow_args: Dict[str, Any]
) -> None:
    """Ensure valid inputs are formatted and replaced as expected."""
    formatted = activities.get_formatted_query(BASE_QUERY, base_workflow_args)

    assert (
        formatted
        == "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE WHERE START_TIME >= 1700000000 "
        "AND START_TIME >= 1700000001 AND START_TIME <= 1700000100"
    )


def test_get_formatted_query_rejects_injected_identifier(
    activities: SQLQueryExtractionActivities, base_workflow_args: Dict[str, Any]
) -> None:
    """Reject schema identifiers containing SQL injection payloads."""
    base_workflow_args["miner_args"]["schema_name_cleaned"] = (
        "users; DROP TABLE users; --"
    )

    with pytest.raises(ValidationError):
        activities.get_formatted_query(BASE_QUERY, base_workflow_args)


def test_get_formatted_query_rejects_dangerous_replace_fragment(
    activities: SQLQueryExtractionActivities, base_workflow_args: Dict[str, Any]
) -> None:
    """Reject sql_replace_to fragments containing statement terminators."""
    base_workflow_args["miner_args"]["sql_replace_to"] = (
        "START_TIME >= [START_MARKER]; DROP TABLE users"
    )

    with pytest.raises(ValidationError):
        activities.get_formatted_query(BASE_QUERY, base_workflow_args)


def test_get_formatted_query_rejects_non_numeric_markers(
    activities: SQLQueryExtractionActivities, base_workflow_args: Dict[str, Any]
) -> None:
    """Reject start/end markers that are not numeric."""
    base_workflow_args["start_marker"] = "0 OR 1=1"

    with pytest.raises(ValueError, match="start_marker"):
        activities.get_formatted_query(BASE_QUERY, base_workflow_args)
