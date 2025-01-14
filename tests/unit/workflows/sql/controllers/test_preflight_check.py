import json
from typing import Any
from unittest.mock import Mock, patch

import pytest

from application_sdk.clients.sql_client import SQLClient
from application_sdk.workflows.sql.controllers.preflight_check import (
    SQLWorkflowPreflightCheckController,
)


@pytest.fixture
def mock_sql_resource():
    sql_resource = Mock(spec=SQLClient)
    sql_resource.fetch_metadata.return_value = [
        {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
        {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
        {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"},
    ]
    return sql_resource


@pytest.fixture
def controller(mock_sql_resource: Any) -> SQLWorkflowPreflightCheckController:
    controller = SQLWorkflowPreflightCheckController(sql_resource=mock_sql_resource)
    controller.METADATA_SQL = "SELECT * FROM information_schema.tables"
    controller.TABLES_CHECK_SQL = "SELECT COUNT(*) FROM information_schema.tables"
    return controller


async def test_fetch_metadata(
    controller: SQLWorkflowPreflightCheckController, mock_sql_resource: Any
):
    result = await controller.fetch_metadata()

    mock_sql_resource.fetch_metadata.assert_called_once()
    assert len(result) == 3
    assert result[0]["TABLE_CATALOG"] == "db1"
    assert result[0]["TABLE_SCHEMA"] == "schema1"


async def test_fetch_metadata_no_resource():
    controller = SQLWorkflowPreflightCheckController()
    with pytest.raises(ValueError, match="SQL Resource not defined"):
        await controller.fetch_metadata()


async def test_check_schemas_and_databases_success(
    controller: SQLWorkflowPreflightCheckController,
):
    payload = {"form_data": {"include_filter": json.dumps({"db1": ["schema1"]})}}

    result = await controller.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_check_schemas_and_databases_failure(
    controller: SQLWorkflowPreflightCheckController,
):
    payload = {
        "form_data": {"include_filter": json.dumps({"invalid_db": ["invalid_schema"]})}
    }

    result = await controller.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


async def test_check_schemas_and_databases_with_wildcard_success(
    controller: SQLWorkflowPreflightCheckController,
):
    # Test with wildcard for all schemas in db1
    payload = {"form_data": {"include_filter": json.dumps({"^db1$": "*"})}}

    result = await controller.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_check_schemas_and_databases_with_wildcard_invalid_db(
    controller: SQLWorkflowPreflightCheckController,
):
    # Test with wildcard but invalid database
    payload = {"form_data": {"include_filter": json.dumps({"^invalid_db$": "*"})}}

    result = await controller.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


async def test_check_schemas_and_databases_mixed_format(
    controller: SQLWorkflowPreflightCheckController,
):
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

    result = await controller.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_preflight_check_success(controller: SQLWorkflowPreflightCheckController):
    payload = {"form_data": {"include_filter": json.dumps({"db1": ["schema1"]})}}

    with patch.object(controller, "tables_check") as mock_tables_check:
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }

        result = await controller.preflight_check(payload)

        assert "error" not in result
        assert result["databaseSchemaCheck"]["success"] is True
        assert result["tablesCheck"]["success"] is True


async def test_preflight_check_with_wildcard_success(
    controller: SQLWorkflowPreflightCheckController,
):
    payload = {"form_data": {"include_filter": json.dumps({"^db1$": "*"})}}

    with patch.object(controller, "tables_check") as mock_tables_check:
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }

        result = await controller.preflight_check(payload)

        assert "error" not in result
        assert result["databaseSchemaCheck"]["success"] is True
        assert result["tablesCheck"]["success"] is True


async def test_preflight_check_failure(controller: SQLWorkflowPreflightCheckController):
    payload = {"form_data": {"include_filter": json.dumps({"invalid_db": ["schema1"]})}}

    result = await controller.preflight_check(payload)

    assert "error" in result
    assert "Preflight check failed" in result["error"]
