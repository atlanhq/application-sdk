import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.clients.sql import SQLClient
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
    payload = {"metadata": {"include-filter": json.dumps({"db1": ["schema1"]})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_check_schemas_and_databases_failure(handler: SQLHandler):
    payload = {
        "metadata": {"include-filter": json.dumps({"invalid_db": ["invalid_schema"]})}
    }

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


async def test_check_schemas_and_databases_with_wildcard_success(
    handler: SQLHandler,
):
    # Test with wildcard for all schemas in db1
    payload = {"metadata": {"include-filter": json.dumps({"^db1$": "*"})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is True
    assert result["successMessage"] == "Schemas and Databases check successful"
    assert result["failureMessage"] == ""


async def test_check_schemas_and_databases_with_wildcard_invalid_db(
    handler: SQLHandler,
):
    # Test with wildcard but invalid database
    payload = {"metadata": {"include-filter": json.dumps({"^invalid_db$": "*"})}}

    result = await handler.check_schemas_and_databases(payload)

    assert result["success"] is False
    assert "invalid_db database" in result["failureMessage"]


async def test_check_schemas_and_databases_mixed_format(handler: SQLHandler):
    # Test with mix of wildcard and specific schema selections
    payload = {
        "metadata": {
            "include-filter": json.dumps(
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
    payload = {"metadata": {"include-filter": json.dumps({"db1": ["schema1"]})}}

    with patch.object(handler, "tables_check") as mock_tables_check, \
         patch.object(handler, "check_client_version") as mock_version_check:
        
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }
        
        mock_version_check.return_value = {
            "success": True,
            "successMessage": "Version check successful",
            "failureMessage": "",
        }

        result = await handler.preflight_check(payload)

        assert "error" not in result
        assert result["databaseSchemaCheck"]["success"] is True
        assert result["tablesCheck"]["success"] is True
        assert result["versionCheck"]["success"] is True


async def test_preflight_check_with_wildcard_success(handler: SQLHandler):
    payload = {"metadata": {"include-filter": json.dumps({"^db1$": "*"})}}

    with patch.object(handler, "tables_check") as mock_tables_check, \
         patch.object(handler, "check_client_version") as mock_version_check:
        
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }
        
        mock_version_check.return_value = {
            "success": True,
            "successMessage": "Version check successful",
            "failureMessage": "",
        }

        result = await handler.preflight_check(payload)

        assert "error" not in result
        assert result["databaseSchemaCheck"]["success"] is True
        assert result["tablesCheck"]["success"] is True
        assert result["versionCheck"]["success"] is True


async def test_preflight_check_failure(handler: SQLHandler):
    payload = {"metadata": {"include-filter": json.dumps({"invalid_db": ["schema1"]})}}

    result = await handler.preflight_check(payload)

    assert "error" in result
    assert "Preflight check failed" in result["error"]


@pytest.mark.parametrize(
    "client_version,min_version,expected_success",
    [
        ("15.4", "15.0", True),  # Client version higher than minimum
        ("15.0", "15.0", True),  # Client version equal to minimum
        ("14.9", "15.0", False),  # Client version lower than minimum
    ],
)
async def test_check_client_version_comparison(
    handler: SQLHandler, client_version, min_version, expected_success, monkeypatch
):
    # Setup dialect version info
    handler.sql_client.engine.dialect.server_version_info = tuple(
        int(x) for x in client_version.split(".")
    )
    
    # Set minimum version environment variable
    monkeypatch.setenv("ATLAN_SQL_SERVER_MIN_VERSION", min_version)
    
    result = await handler.check_client_version()
    
    assert result["success"] is expected_success
    if expected_success:
        assert f"meets minimum required version" in result["successMessage"]
        assert result["failureMessage"] == ""
    else:
        assert result["successMessage"] == ""
        assert f"does not meet minimum required version" in result["failureMessage"]


async def test_check_client_version_no_minimum_version(handler: SQLHandler, monkeypatch):
    # Setup dialect version info
    handler.sql_client.engine.dialect.server_version_info = (15, 4)
    
    # Ensure environment variable is not set
    monkeypatch.delenv("ATLAN_SQL_SERVER_MIN_VERSION", raising=False)
    
    result = await handler.check_client_version()
    
    assert result["success"] is True
    assert "no minimum version requirement" in result["successMessage"]
    assert result["failureMessage"] == ""


async def test_check_client_version_no_client_version(handler: SQLHandler):
    # Remove server_version_info attribute
    delattr(handler.sql_client.engine.dialect, "server_version_info")
    
    # Ensure get_client_version_sql is not defined
    handler.get_client_version_sql = None
    
    result = await handler.check_client_version()
    
    assert result["success"] is False
    assert "error" in result
    assert "Client version check failed" in result["failureMessage"]


async def test_check_client_version_sql_query(handler: SQLHandler, monkeypatch):
    # Remove server_version_info attribute
    delattr(handler.sql_client.engine.dialect, "server_version_info")
    
    # Set up SQL query for version
    handler.get_client_version_sql = "SELECT version();"
    
    # Mock SQLQueryInput.get_dataframe to return a DataFrame with version
    mock_df = Mock()
    mock_df.to_dict.return_value = {"records": [{"version": "PostgreSQL 15.4 on x86_64-pc-linux-gnu"}]}
    
    with patch("application_sdk.inputs.sql_query.SQLQueryInput", 
               new_callable=AsyncMock) as mock_sql_input:
        # Configure the mock to return our mock dataframe
        mock_instance = mock_sql_input.return_value
        mock_instance.get_dataframe.return_value = mock_df
        
        # Set minimum version environment variable
        monkeypatch.setenv("ATLAN_SQL_SERVER_MIN_VERSION", "15.0")
        
        result = await handler.check_client_version()
        
        assert result["success"] is False
        assert "error" in result


async def test_check_client_version_exception(handler: SQLHandler):
    # Force an exception during version check
    with patch.object(handler.sql_client.engine.dialect, "server_version_info",
                     side_effect=Exception("Test exception")):
        result = await handler.check_client_version()
        
        assert result["success"] is True
        assert "version could not be determined" in result["successMessage"]
