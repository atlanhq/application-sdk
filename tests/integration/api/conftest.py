from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.workflows.sql.controllers.metadata import (
    SQLWorkflowMetadataController,
)
from application_sdk.workflows.sql.controllers.preflight_check import (
    SQLWorkflowPreflightCheckController,
)


@pytest.fixture(autouse=True, scope="session")
def mock_sql_resource() -> Any:
    mock = Mock()
    mock.fetch_metadata = AsyncMock()
    mock.sql_input = AsyncMock()
    return mock


@pytest.fixture
def metadata_controller(mock_sql_resource: Any) -> SQLWorkflowMetadataController:
    controller = SQLWorkflowMetadataController(mock_sql_resource)
    return controller


@pytest.fixture
def preflight_check_controller(
    mock_sql_resource: Any,
) -> SQLWorkflowPreflightCheckController:
    controller = SQLWorkflowPreflightCheckController(mock_sql_resource)
    controller.TABLES_CHECK_SQL = """
        SELECT count(*) as "count"
        FROM ACCOUNT_USAGE.TABLES
        WHERE NOT TABLE_NAME RLIKE '{exclude_table}'
            AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_include_regex}'
    """
    return controller


@pytest.fixture
def app(
    metadata_controller: SQLWorkflowMetadataController,
    preflight_check_controller: SQLWorkflowPreflightCheckController,
) -> FastAPIApplication:
    """Create FastAPI test application"""
    app = FastAPIApplication(
        metadata_controller=metadata_controller,
        preflight_check_controller=preflight_check_controller,
    )
    app.register_routers()
    app.register_routes()
    return app


@pytest.fixture
def client(app: FastAPIApplication) -> TestClient:
    """Create test client"""
    return TestClient(app.app)
