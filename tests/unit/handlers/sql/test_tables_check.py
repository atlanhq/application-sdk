from typing import Dict, List

import pytest

from application_sdk.handlers.sql import SQLHandler


class TestExtractAllowedSchemas:
    @pytest.fixture
    def handler(self) -> SQLHandler:
        handler = SQLHandler()
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"
        return handler

    def test_single_schema(self, handler: SQLHandler) -> None:
        """Test extraction with a single schema"""
        schemas_results: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        ]
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == {"db1"}
        assert allowed_schemas == {"db1.schema1"}

    def test_multiple_schemas_same_database(self, handler: SQLHandler) -> None:
        """Test extraction with multiple schemas in the same database"""
        schemas_results: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
        ]
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == {"db1"}
        assert allowed_schemas == {"db1.schema1", "db1.schema2"}

    def test_multiple_databases(self, handler: SQLHandler) -> None:
        """Test extraction with multiple databases"""
        schemas_results: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"},
        ]
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == {"db1", "db2"}
        assert allowed_schemas == {"db1.schema1", "db2.schema1"}

    def test_empty_results(self, handler: SQLHandler) -> None:
        """Test extraction with empty results"""
        schemas_results: List[Dict[str, str]] = []
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == set()
        assert allowed_schemas == set()

    def test_complex_scenario(self, handler: SQLHandler) -> None:
        """Test extraction with a complex mix of databases and schemas"""
        schemas_results: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
            {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db3", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db3", "TABLE_SCHEMA": "schema2"},
        ]
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == {"db1", "db2", "db3"}
        assert allowed_schemas == {
            "db1.schema1",
            "db1.schema2",
            "db2.schema1",
            "db3.schema1",
            "db3.schema2",
        }

    def test_custom_result_keys(self, handler: SQLHandler) -> None:
        """Test extraction with custom database and schema result keys"""
        handler.database_result_key = "DATABASE"
        handler.schema_result_key = "SCHEMA"
        
        schemas_results: List[Dict[str, str]] = [
            {"DATABASE": "db1", "SCHEMA": "schema1"},
            {"DATABASE": "db1", "SCHEMA": "schema2"},
        ]
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == {"db1"}
        assert allowed_schemas == {"db1.schema1", "db1.schema2"}

    def test_duplicate_entries(self, handler: SQLHandler) -> None:
        """Test extraction with duplicate entries (should be deduplicated by set)"""
        schemas_results: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},  # Duplicate entry
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
        ]
        allowed_databases, allowed_schemas = handler.extract_allowed_schemas(schemas_results)
        
        assert allowed_databases == {"db1"}
        assert allowed_schemas == {"db1.schema1", "db1.schema2"}
