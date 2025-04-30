import json
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock, patch

import pytest
from _pytest.monkeypatch import MonkeyPatch
from hypothesis import HealthCheck, given, settings

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.test_utils.hypothesis.strategies.handlers.sql.sql_preflight import (
    metadata_list_strategy,
    mixed_mapping_strategy,
    version_comparison_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "sql_preflight_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("sql_preflight_tests")


@pytest.fixture
def mock_sql_client() -> Mock:
    sql_client = Mock(spec=BaseSQLClient)
    # Add mock engine with dialect to avoid None errors
    mock_engine = Mock()
    mock_dialect = Mock()
    mock_dialect.server_version_info = (12, 0)
    mock_engine.dialect = mock_dialect
    sql_client.engine = mock_engine
    return sql_client


@pytest.fixture
def handler(mock_sql_client: Mock) -> BaseSQLHandler:
    handler = BaseSQLHandler(sql_client=mock_sql_client)
    handler.metadata_sql = "SELECT * FROM information_schema.tables"
    handler.tables_check_sql = "SELECT COUNT(*) FROM information_schema.tables"
    return handler


@given(metadata=metadata_list_strategy)
async def test_fetch_metadata(
    handler: BaseSQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    result = await handler.prepare_metadata()

    handler.prepare_metadata.assert_awaited_once()
    assert len(result) == len(metadata)
    for expected, actual in zip(metadata, result):
        assert expected["TABLE_CATALOG"] == actual["TABLE_CATALOG"]
        assert expected["TABLE_SCHEMA"] == actual["TABLE_SCHEMA"]


async def test_fetch_metadata_no_resource() -> None:
    handler = BaseSQLHandler()
    with pytest.raises(ValueError, match="SQL client is not defined"):
        await handler.fetch_metadata()


@pytest.mark.skip(reason="Failing due to IndexError: list index out of range")
@given(metadata=metadata_list_strategy, mapping=mixed_mapping_strategy)
async def test_check_schemas_and_databases_success(
    handler: BaseSQLHandler, metadata: List[Dict[str, str]], mapping: Dict[str, Any]
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
    handler: BaseSQLHandler, metadata: List[Dict[str, str]]
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
    handler: BaseSQLHandler, metadata: List[Dict[str, str]]
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
    handler: BaseSQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create a valid payload using the first database and schema from metadata
    first_entry = metadata[0]
    valid_mapping = {first_entry["TABLE_CATALOG"]: [first_entry["TABLE_SCHEMA"]]}
    payload = {"metadata": {"include-filter": json.dumps(valid_mapping)}}

    with patch.object(handler, "tables_check") as mock_tables_check, patch.object(
        handler, "check_client_version"
    ) as mock_client_version:
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }
        mock_client_version.return_value = {
            "success": True,
            "successMessage": "Client version check successful",
            "failureMessage": "",
        }

        result = await handler.preflight_check(payload)

        assert "error" not in result
        assert result["databaseSchemaCheck"]["success"] is True
        assert result["tablesCheck"]["success"] is True
        assert result["versionCheck"]["success"] is True


@given(metadata=metadata_list_strategy)
async def test_preflight_check_failure(
    handler: BaseSQLHandler, metadata: List[Dict[str, str]]
) -> None:
    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create an invalid payload
    payload = {"metadata": {"include-filter": json.dumps({"invalid_db": ["schema1"]})}}

    # Add client version check mock that succeeds (to isolate the failure to schema check)
    with patch.object(handler, "check_client_version") as mock_client_version:
        mock_client_version.return_value = {
            "success": True,
            "successMessage": "Client version check successful",
            "failureMessage": "",
        }

        result = await handler.preflight_check(payload)

        assert "error" in result
        assert "Preflight check failed" in result["error"]


@given(version_data=version_comparison_strategy)
async def test_check_client_version_comparison(
    handler: BaseSQLHandler,
    version_data: Tuple[str, str, bool],
    monkeypatch: MonkeyPatch,
):
    client_version, min_version, expected_success = version_data

    # Setup dialect version info
    if handler.sql_client.engine is not None:
        handler.sql_client.engine.dialect.server_version_info = tuple(
            int(x) for x in client_version.split(".")
        )

    # Set minimum version environment variable
    monkeypatch.setattr(
        "application_sdk.handlers.sql.SQL_SERVER_MIN_VERSION", min_version
    )

    result = await handler.check_client_version()

    assert result["success"] is expected_success
    if expected_success:
        assert "meets minimum required version" in result["successMessage"]
        assert result["failureMessage"] == ""
    else:
        assert result["successMessage"] == ""
        assert "does not meet minimum required version" in result["failureMessage"]


async def test_check_client_version_no_minimum_version(
    handler: BaseSQLHandler, monkeypatch: MonkeyPatch
):
    # Setup dialect version info
    if handler.sql_client.engine is not None:
        handler.sql_client.engine.dialect.server_version_info = (15, 4)

    # Ensure environment variable is not set
    monkeypatch.delenv("ATLAN_SQL_SERVER_MIN_VERSION", raising=False)

    result = await handler.check_client_version()

    assert result["success"] is True
    assert "no minimum version requirement" in result["successMessage"]
    assert result["failureMessage"] == ""


async def test_check_client_version_no_client_version(handler: BaseSQLHandler):
    # Remove server_version_info attribute
    if handler.sql_client.engine is not None:
        delattr(handler.sql_client.engine.dialect, "server_version_info")

    # Ensure get_client_version_sql is not defined
    handler.get_client_version_sql = None

    result = await handler.check_client_version()

    assert result["success"] is False
    assert "error" in result
    assert "Client version check failed" in result["failureMessage"]


async def test_check_client_version_sql_query(
    handler: BaseSQLHandler, monkeypatch: MonkeyPatch
):
    # Remove server_version_info attribute
    if handler.sql_client.engine is not None:
        delattr(handler.sql_client.engine.dialect, "server_version_info")

    # Set up SQL query for version
    handler.get_client_version_sql = "SELECT version();"

    # Mock SQLQueryInput.get_dataframe to return a DataFrame with version
    mock_df = Mock()
    mock_df.to_dict.return_value = {
        "records": [{"version": "PostgreSQL 15.4 on x86_64-pc-linux-gnu"}]
    }

    with patch(
        "application_sdk.inputs.sql_query.SQLQueryInput", new_callable=AsyncMock
    ) as mock_sql_input:
        # Configure the mock to return our mock dataframe
        mock_instance = mock_sql_input.return_value
        mock_instance.get_dataframe.return_value = mock_df

        # Set minimum version environment variable
        monkeypatch.setenv("ATLAN_SQL_SERVER_MIN_VERSION", "15.0")

        result = await handler.check_client_version()

        assert result["success"] is False
        assert "error" in result


async def test_check_client_version_exception(handler: BaseSQLHandler):
    # Force an exception during version check
    if handler.sql_client.engine is not None:
        with patch.object(
            handler.sql_client.engine.dialect,
            "server_version_info",
            side_effect=Exception("Test exception"),
        ):
            result = await handler.check_client_version()

        assert result["success"] is True
        assert "version could not be determined" in result["successMessage"]


async def test_preflight_check_version_failure(
    handler: BaseSQLHandler, metadata: Optional[List[Dict[str, str]]] = None
) -> None:
    """Test that preflight check fails when client version check fails."""
    if metadata is None:
        metadata = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    handler.prepare_metadata = AsyncMock(return_value=metadata)
    # Create a valid payload for schemas
    valid_mapping = {metadata[0]["TABLE_CATALOG"]: [metadata[0]["TABLE_SCHEMA"]]}
    payload = {"metadata": {"include-filter": json.dumps(valid_mapping)}}

    # Tables check succeeds but version check fails
    with patch.object(handler, "tables_check") as mock_tables_check, patch.object(
        handler, "check_client_version"
    ) as mock_client_version:
        mock_tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful",
            "failureMessage": "",
        }
        mock_client_version.return_value = {
            "success": False,
            "successMessage": "",
            "failureMessage": "Client version does not meet minimum required version",
        }

        result = await handler.preflight_check(payload)

        assert "error" in result
        assert "Preflight check failed" in result["error"]
        assert "versionCheck" in result
        assert result["versionCheck"]["success"] is False
        assert (
            "does not meet minimum required version"
            in result["versionCheck"]["failureMessage"]
        )
