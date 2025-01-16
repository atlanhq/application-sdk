import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.clients.sql_client import SQLClient
from application_sdk.handlers.sql import SQLHandler


@pytest.fixture
def mock_sql_client():
    sql_client = Mock(spec=SQLClient)
    return sql_client


@pytest.fixture
def handler(mock_sql_client: Any) -> SQLHandler:
    handler = SQLHandler(sql_client=mock_sql_client)
    handler.metadata_sql = "SELECT * FROM information_schema.tables"
    handler.tables_check_sql = "SELECT COUNT(*) FROM information_schema.tables"
    handler.prepare_metadata = AsyncMock()
    handler.prepare_metadata.return_value = [
        {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
        {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
        {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"},
    ]
    return handler


async def test_fetch_metadata(handler: SQLHandler):
    result = await handler.prepare_metadata()

    handler.prepare_metadata.assert_called_once()
    assert len(result) == 3
    assert result[0]["TABLE_CATALOG"] == "db1"
    assert result[0]["TABLE_SCHEMA"] == "schema1"


async def test_fetch_metadata_no_resource():
    handler = SQLHandler()
    with pytest.raises(ValueError, match="SQL client is not defined"):
        await handler.fetch_metadata()


async def test_check_schemas_and_databases_success(handler: SQLHandler):
    payload = {"form_data": {"include_filter": json.dumps({"db1": ["schema1"]})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_check_schemas_and_databases_failure(handler: SQLHandler):
    payload = {
        "form_data": {"include_filter": json.dumps({"invalid_db": ["invalid_schema"]})}
    }

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


async def test_check_schemas_and_databases_with_wildcard_success(
    handler: SQLHandler,
):
    # Test with wildcard for all schemas in db1
    payload = {"form_data": {"include_filter": json.dumps({"^db1$": "*"})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_check_schemas_and_databases_with_wildcard_invalid_db(
    handler: SQLHandler,
):
    # Test with wildcard but invalid database
    payload = {"form_data": {"include_filter": json.dumps({"^invalid_db$": "*"})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


async def test_check_schemas_and_databases_mixed_format(handler: SQLHandler):
    # Test with mix of wildcard and specific schema selections
    payload = {
        "form_data": {
            "include_filter": json.dumps(
                {
                    "^db1$": "*",  # All schemas in db1
                    "^db2$": ["schema1"],  # Specific schema in db2
                }
            )
        }
    }

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_preflight_check_success(handler: SQLHandler):
    payload = {"form_data": {"include_filter": json.dumps({"db1": ["schema1"]})}}

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


async def test_preflight_check_with_wildcard_success(handler: SQLHandler):
    payload = {"form_data": {"include_filter": json.dumps({"^db1$": "*"})}}

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


async def test_preflight_check_failure(handler: SQLHandler):
    payload = {"form_data": {"include_filter": json.dumps({"invalid_db": ["schema1"]})}}

    result = await handler.preflight_check(payload)

    assert "error" in result
    assert "Preflight check failed" in result["error"]
