import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.clients.sql import SQLClient
from application_sdk.handlers.sql import SQLHandler
from application_sdk.test_utils.hypothesis.strategies.handlers.sql.sql_preflight import (
    metadata_list_strategy,
    mixed_mapping_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "sql_preflight_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("sql_preflight_tests")


@pytest.fixture
def mock_sql_client() -> Mock:
    sql_client = Mock(spec=SQLClient)
    return sql_client


@pytest.fixture
def handler(mock_sql_client: Mock) -> SQLHandler:
    handler = SQLHandler(sql_client=mock_sql_client)
    handler.metadata_sql = "SELECT * FROM information_schema.tables"
    handler.tables_check_sql = "SELECT COUNT(*) FROM information_schema.tables"
    return handler


@given(metadata=metadata_list_strategy)
async def test_fetch_metadata(
    handler: SQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    result = await handler.prepare_metadata()

    handler.prepare_metadata.assert_awaited_once()
    assert len(result) == len(metadata)
    for expected, actual in zip(metadata, result):
        assert expected["TABLE_CATALOG"] == actual["TABLE_CATALOG"]
        assert expected["TABLE_SCHEMA"] == actual["TABLE_SCHEMA"]


async def test_fetch_metadata_no_resource() -> None:
    handler = SQLHandler()
    with pytest.raises(ValueError, match="SQL client is not defined"):
        await handler.fetch_metadata()


@pytest.mark.skip(reason="Failing due to IndexError: list index out of range")
@given(metadata=metadata_list_strategy, mapping=mixed_mapping_strategy)
async def test_check_schemas_and_databases_success(
    handler: SQLHandler, metadata: List[Dict[str, str]], mapping: Dict[str, Any]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create a payload with a database and schema that exists in the metadata
    first_entry = metadata[0]
    db_name = first_entry["TABLE_CATALOG"]
    schema_name = first_entry["TABLE_SCHEMA"]

    # Create a valid mapping using the first database and schema
    valid_mapping = {db_name: [schema_name]}
    payload = {"metadata": {"include-filter": json.dumps(valid_mapping)}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


@given(metadata=metadata_list_strategy)
async def test_check_schemas_and_databases_failure(
    handler: SQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create an invalid mapping that doesn't exist in metadata
    invalid_mapping = {"invalid_db": ["invalid_schema"]}
    payload = {"metadata": {"include-filter": json.dumps(invalid_mapping)}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


@pytest.mark.skip(reason="Failing due to IndexError: list index out of range")
@given(metadata=metadata_list_strategy)
async def test_check_schemas_and_databases_with_wildcard_success(
    handler: SQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Use the first database from metadata with wildcard schema
    first_db = metadata[0]["TABLE_CATALOG"]
    payload = {"metadata": {"include-filter": json.dumps({f"^{first_db}$": "*"})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


@pytest.mark.skip(reason="Failing due to IndexError: list index out of range")
@given(metadata=metadata_list_strategy)
async def test_preflight_check_success(
    handler: SQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create a valid payload using the first database and schema from metadata
    first_entry = metadata[0]
    valid_mapping = {first_entry["TABLE_CATALOG"]: [first_entry["TABLE_SCHEMA"]]}
    payload = {"metadata": {"include-filter": json.dumps(valid_mapping)}}

    with patch.object(handler, "tables_check") as mock_tables_check:
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }

        result = await handler.preflight_check(payload)

        assert "error" not in result
        assert result["databaseSchemaCheck"]["success"] is True
        assert result["tablesCheck"]["success"] is True


@given(metadata=metadata_list_strategy)
async def test_preflight_check_failure(
    handler: SQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create an invalid payload
    payload = {"metadata": {"include-filter": json.dumps({"invalid_db": ["schema1"]})}}

    result = await handler.preflight_check(payload)

    assert "error" in result
    assert "Preflight check failed" in result["error"]
