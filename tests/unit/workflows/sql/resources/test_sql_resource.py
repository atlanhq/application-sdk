import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)


@pytest.fixture
def config():
    # Create a sample SQLResourceConfig object with mock credentials
    return SQLResourceConfig(
        credentials={
            "user": "test_user",
            "password": "test_password",
            "host": "localhost",
            "port": 5432,
            "database": "test_db",
        },
        sql_alchemy_connect_args={},
    )


@pytest.fixture
def resource(config: SQLResourceConfig):
    # Create a SQLResource object with the above config
    return SQLResource(config=config)


async def async_gen(_):
    yield [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]


@patch("application_sdk.workflows.sql.resources.sql_resource.create_engine")
def test_load(mock_create_engine: Any, resource: SQLResource):
    # Mock the engine and connection
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    mock_create_engine.return_value = mock_engine
    mock_engine.connect.return_value = mock_connection

    # Run the load function
    asyncio.run(resource.load())

    # Assertions to verify behavior
    mock_create_engine.assert_called_once_with(
        resource.config.get_sqlalchemy_connection_string(),
        connect_args=resource.config.get_sqlalchemy_connect_args(),
        pool_pre_ping=True,
    )
    assert resource.engine == mock_engine
    assert resource.connection == mock_connection


@patch("application_sdk.workflows.sql.resources.sql_resource.SQLResource.run_query")
async def test_fetch_metadata(mock_run_query: Any, resource: SQLResource):
    mock_run_query.side_effect = async_gen

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    result = await resource.fetch_metadata(
        metadata_sql,
        database_alias_key="TABLE_CATALOG",
        schema_alias_key="TABLE_SCHEMA",
    )

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with(metadata_sql)
