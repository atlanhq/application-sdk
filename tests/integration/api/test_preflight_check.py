import json

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
        handler.prepare_metadata.assert_called_once()

        # Verify the SQL query was generated correctly
        expected_sql = """
        SELECT count(*) as "count"
            FROM ACCOUNT_USAGE.TABLES
            WHERE NOT TABLE_NAME RLIKE '^$'
                AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
        """

        prepared_sql = prepare_query(handler.tables_check_sql, payload)
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_empty_filters(
        self,
        client: TestClient,
        handler: SQLHandler,
    ):
        """Test the /check endpoint with empty filters"""

        handler.prepare_metadata.return_value = []

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
                "include_filter": "{}",
                "exclude_filter": "{}",
                "temp_table_regex": "",
            },
        }

        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        expected_sql = """
            SELECT count(*) as "count"
            FROM ACCOUNT_USAGE.TABLES
            WHERE NOT TABLE_NAME RLIKE '^$'
                AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '^$'
                AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE '.*'
        """

        prepared_sql = prepare_query(handler.tables_check_sql, payload)
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)

    async def test_check_endpoint_both_filters(
        self,
        client: TestClient,
        handler: SQLHandler,
    ):
        """Test the /check endpoint with both filters"""

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
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "metadata": {
                "include_filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude_filter": json.dumps({"^TESTDB$": ["^PRIVATE$"]}),
                "temp_table_regex": "",
            },
        }

        response = client.post("/workflows/v1/check", json=payload)
        assert response.status_code == 200

        expected_sql = """
           SELECT count(*) as "count"
           FROM ACCOUNT_USAGE.TABLES
           WHERE NOT TABLE_NAME RLIKE '^$'
            AND NOT concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PRIVATE$'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) RLIKE 'TESTDB\.PUBLIC$'
        """

        prepared_sql = prepare_query(handler.tables_check_sql, payload)
        assert self.normalize_sql(prepared_sql) == self.normalize_sql(expected_sql)
