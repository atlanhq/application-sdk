from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.handlers.sql import SQLWorkflowHandler


@pytest.fixture(autouse=True, scope="session")
def mock_sql_resource() -> Any:
    mock = Mock()
    mock.sql_input = AsyncMock()
    return mock


@pytest.fixture(autouse=True, scope="session")
def handler(mock_sql_resource: Any) -> SQLWorkflowHandler:
    handler = SQLWorkflowHandler(mock_sql_resource)
    handler.prepare_metadata = AsyncMock()
    handler.tables_check = AsyncMock()
    handler.TABLES_CHECK_SQL = """
        SELECT count(*) as "count"
        FROM ACCOUNT_USAGE.TABLES
        WHERE NOT TABLE_NAME RLIKE '{exclude_table}'
            AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_include_regex}'
    """
    return handler


@pytest.fixture
def app(
    handler: SQLWorkflowHandler,
) -> FastAPIApplication:
    """Create FastAPI test application"""
    app = FastAPIApplication(handler=handler)
    app.register_routers()
    app.register_routes()
    return app


@pytest.fixture
def client(app: FastAPIApplication) -> TestClient:
    """Create test client"""
    return TestClient(app.app)
