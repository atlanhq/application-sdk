import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.workflows.sql.controllers.preflight_check import (
    SQLWorkflowPreflightCheckController,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow


class TestSQLPreflightCheck:
    @pytest.fixture
    def mock_sql_resource(self) -> Any:
        mock = Mock()
        mock.fetch_metadata = AsyncMock()
        mock.sql_input = AsyncMock()
        return mock

    @pytest.fixture
    def controller(self, mock_sql_resource: Any) -> SQLWorkflowPreflightCheckController:
        controller = SQLWorkflowPreflightCheckController(mock_sql_resource)
        controller.TABLES_CHECK_SQL = """
            SELECT count(*) as "count"
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
            WHERE NOT TABLE_NAME RLIKE '{exclude_table}'
                AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_exclude_regex}'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '{normalized_include_regex}'
        """
        return controller

    @pytest.fixture
    def app(
        self, controller: SQLWorkflowPreflightCheckController
    ) -> FastAPIApplication:
        """Create FastAPI test application"""
        app = FastAPIApplication(preflight_check_controller=controller)
        app.register_routers()
        app.register_routes()
        return app

    @pytest.fixture
    def client(self, app: FastAPIApplication) -> TestClient:
        """Create test client"""
        return TestClient(app.app)

    def normalize_sql(self, sql: str) -> str:
        """Helper to normalize SQL for comparison"""
        return " ".join(sql.split()).strip().lower()

    async def test_check_endpoint_basic_filters(self, client, controller):
        """Test the complete flow from /check endpoint through to SQL generation"""

        # Setup mock for sql_resource.fetch_metadata
        controller.sql_resource.fetch_metadata.return_value = [
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PUBLIC"}
        ]

        payload = {
            "credentials": {
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "form_data": {
                "include_filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude_filter": "{}",
                "temp_table_regex": "",
            },
        }

        # Call the /check endpoint
        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        # Verify the response structure
        response_data = response.json()
        assert response_data["success"] is True
        assert "data" in response_data

        # Verify that preflight_check was called with correct args
        controller.sql_resource.fetch_metadata.assert_called_once()

        # Verify the SQL query was generated correctly
        expected_sql = """
        SELECT count(*) as "count"
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
            WHERE NOT TABLE_NAME RLIKE '$^'
                AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '$^'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
        """

        prepared_sql = SQLWorkflow.prepare_query(controller.TABLES_CHECK_SQL, payload)
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_empty_filters(self, client, controller):
        """Test the /check endpoint with empty filters"""

        controller.sql_resource.fetch_metadata.return_value = []

        payload = {
            "credentials": {
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "form_data": {
                "include_filter": "{}",
                "exclude_filter": "{}",
                "temp_table_regex": "",
            },
        }

        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        expected_sql = """
            SELECT count(*) as "count"
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
            WHERE NOT TABLE_NAME RLIKE ''
                AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '.*'
        """

        prepared_sql = SQLWorkflow.prepare_query(controller.TABLES_CHECK_SQL, payload)
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_both_filters(self, client, controller):
        """Test the /check endpoint with both filters"""

        controller.sql_resource.fetch_metadata.return_value = [
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PUBLIC"},
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PRIVATE"},
        ]

        payload = {
            "credentials": {
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "form_data": {
                "include_filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude_filter": json.dumps({"^TESTDB$": ["^PRIVATE$"]}),
                "temp_table_regex": "",
            },
        }

        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        expected_sql = """
           SELECT count(*) as "count"
           FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
           WHERE NOT TABLE_NAME RLIKE '$^'
            AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PRIVATE$'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
        """

        prepared_sql = SQLWorkflow.prepare_query(controller.TABLES_CHECK_SQL, payload)
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)
