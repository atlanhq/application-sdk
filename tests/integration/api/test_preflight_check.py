import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from application_sdk.common.utils import prepare_query
from application_sdk.handlers.sql import SQLHandler


class TestSQLPreflightCheck:
    def normalize_sql(self, sql: str) -> str:
        """Helper to normalize SQL for comparison"""
        return " ".join(sql.split()).strip().lower()

    async def test_check_endpoint_basic_filters(
        self,
        client: TestClient,
        handler: SQLHandler,
    ):
        """Test the complete flow from /check endpoint through to SQL generation"""
        # Make sure load is properly mocked as AsyncMock
        handler.load = AsyncMock()

        # Setup mock for handler.prepare_metadata
        handler.prepare_metadata.return_value = [
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PUBLIC"}
        ]

        handler.tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful. Table count: 1",
            "failureMessage": "",
        }

        payload = {
            "credentials": {
                "authType": "basic",
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "metadata": {
                "include-filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude-filter": "{}",
                "temp-table-regex": "",
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
        handler.prepare_metadata.assert_called_once()
        handler.load.assert_called_once()

        # Verify the SQL query was generated correctly
        expected_sql = """
        SELECT count(*) as "count"
            FROM ACCOUNT_USAGE.TABLES
            WHERE NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
        """

        prepared_sql = prepare_query(
            handler.tables_check_sql,
            payload,
            temp_table_regex_sql="AND NOT TABLE_NAME RLIKE '{exclude_table_regex}'",
        )
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_basic_filters_with_temp_table_regex(
        self,
        client: TestClient,
        handler: SQLHandler,
    ):
        """Test the complete flow from /check endpoint through to SQL generation"""
        # Make sure load is properly mocked as AsyncMock
        handler.load = AsyncMock()
        handler.prepare_metadata.call_count = 0

        # Setup mock for handler.prepare_metadata
        handler.prepare_metadata.return_value = [
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PUBLIC"}
        ]

        handler.tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful. Table count: 1",
            "failureMessage": "",
        }

        payload = {
            "credentials": {
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "metadata": {
                "include-filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude-filter": "{}",
                "temp-table-regex": "^TEMP_TABLE$",
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
        handler.prepare_metadata.assert_called_once()
        handler.load.assert_called_once()

        # Verify the SQL query was generated correctly
        expected_sql = """
        SELECT count(*) as "count"
            FROM ACCOUNT_USAGE.TABLES
            WHERE NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
                AND NOT TABLE_NAME RLIKE '^TEMP_TABLE$'
        """

        prepared_sql = prepare_query(
            handler.tables_check_sql,
            payload,
            temp_table_regex_sql="AND NOT TABLE_NAME RLIKE '{exclude_table_regex}'",
        )
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_empty_filters(
        self,
        client: TestClient,
        handler: SQLHandler,
    ):
        """Test the /check endpoint with empty filters"""
        # Make sure load is properly mocked as AsyncMock
        handler.load = AsyncMock()

        handler.prepare_metadata.return_value = []

        handler.tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful. Table count: 1",
            "failureMessage": "",
        }

        payload = {
            "credentials": {
                "authType": "basic",
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "metadata": {
                "include-filter": "{}",
                "exclude-filter": "{}",
                "temp-table-regex": "",
            },
        }

        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        # Verify load was called
        handler.load.assert_called_once()

        expected_sql = """
            SELECT count(*) as "count"
            FROM ACCOUNT_USAGE.TABLES
            WHERE NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '.*'
        """

        prepared_sql = prepare_query(
            handler.tables_check_sql,
            payload,
            temp_table_regex_sql="AND NOT TABLE_NAME RLIKE '{exclude_table_regex}'",
        )
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_both_filters(
        self,
        client: TestClient,
        handler: SQLHandler,
    ):
        """Test the /check endpoint with both filters"""
        # Make sure load is properly mocked as AsyncMock
        handler.load = AsyncMock()

        handler.prepare_metadata.return_value = [
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PUBLIC"},
            {"TABLE_CATALOG": "TESTDB", "TABLE_SCHEMA": "PRIVATE"},
        ]

        handler.tables_check.return_value = {
            "success": True,
            "successMessage": "Tables check successful. Table count: 1",
            "failureMessage": "",
        }

        payload = {
            "credentials": {
                "authType": "basic",
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "metadata": {
                "include-filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude-filter": json.dumps({"^TESTDB$": ["^PRIVATE$"]}),
                "temp-table-regex": "",
            },
        }

        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        # Verify load was called
        handler.load.assert_called_once()

        expected_sql = """
           SELECT count(*) as "count"
           FROM ACCOUNT_USAGE.TABLES
           WHERE NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PRIVATE$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
        """

        prepared_sql = prepare_query(
            handler.tables_check_sql,
            payload,
            temp_table_regex_sql="AND NOT TABLE_NAME RLIKE '{exclude_table_regex}'",
        )
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)
