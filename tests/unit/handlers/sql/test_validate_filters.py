from typing import Dict, List, Set

import pytest

from application_sdk.handlers.sql import BaseSQLHandler


class TestValidateFilters:
    @pytest.fixture
    def allowed_databases(self) -> Set[str]:
        return {"db1", "db2", "db3"}

    @pytest.fixture
    def allowed_schemas(self) -> Set[str]:
        return {
            "db1.schema1",
            "db1.schema2",
            "db2.schema1",
            "db3.schema1",
            "db3.schema2",
        }

    def test_valid_database_and_schema(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with valid database and schema combinations"""
        include_filter: Dict[str, List[str] | str] = {
            "^db1$": ["^schema1$", "^schema2$"]
        }
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is True
        assert message == ""

    def test_invalid_database(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with invalid database"""
        include_filter: Dict[str, List[str] | str] = {
            "^invalid_db$": ["^schema1$"]
        }  # invlid because invalid_db is not in allowed_databases
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is False
        assert message == "invalid_db database"

    def test_invalid_schema(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with valid database but invalid schema"""
        include_filter: Dict[str, List[str] | str] = {
            "^db1$": ["^invalid_schema$"]
        }  # invalid because invalid_schema is not in allowed_schemas
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is False
        assert message == "db1.invalid_schema schema"

    def test_wildcard_schema(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with wildcard schema"""
        include_filter: Dict[str, List[str] | str] = {"^db1$": "*"}
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is True
        assert message == ""

    def test_multiple_databases(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with multiple databases"""
        include_filter: Dict[str, List[str] | str] = {
            "^db1$": ["^schema1$"],
            "^db2$": ["^schema1$"],
        }
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is True
        assert message == ""

    def test_mixed_valid_and_invalid(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with mix of valid and invalid entries"""
        include_filter: Dict[str, List[str] | str] = {
            "^db1$": ["^schema1$"],
            "^invalid_db$": ["^schema1$"],
        }
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is False
        assert message == "invalid_db database"

    def test_empty_filter(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with empty filter"""
        include_filter: Dict[str, List[str] | str] = {}
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is True
        assert message == ""

    def test_mixed_wildcard_and_specific(
        self, allowed_databases: Set[str], allowed_schemas: Set[str]
    ) -> None:
        """Test validation with mix of wildcard and specific schema entries"""
        include_filter: Dict[str, List[str] | str] = {
            "^db1$": "*",
            "^db2$": ["^schema1$"],
        }
        success, message = BaseSQLHandler.validate_filters(
            include_filter, allowed_databases, allowed_schemas
        )
        assert success is True
        assert message == ""
