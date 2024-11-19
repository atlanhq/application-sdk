from typing import Any, Dict

import pytest


@pytest.fixture
def sample_database_data() -> Dict[str, Any]:
    return {"database_name": "test_db", "schema_count": 5}


@pytest.fixture
def sample_schema_data() -> Dict[str, Any]:
    return {
        "schema_name": "test_schema",
        "catalog_name": "test_catalog",
        "table_count": 10,
        "view_count": 2,
    }


@pytest.fixture
def sample_table_data() -> Dict[str, Any]:
    return {
        "table_name": "test_table",
        "table_catalog": "test_catalog",
        "table_schema": "test_schema",
        "table_type": "TABLE",
        "column_count": 5,
        "row_count": 1000,
        "size_bytes": 1024,
    }


@pytest.fixture
def sample_view_data() -> Dict[str, Any]:
    return {
        "table_name": "test_view",
        "table_catalog": "test_catalog",
        "table_schema": "test_schema",
        "table_type": "VIEW",
        "VIEW_DEFINITION": "SELECT * FROM test_table",
        "column_count": 3,
    }


@pytest.fixture
def sample_column_data() -> Dict[str, Any]:
    return {
        "column_name": "test_column",
        "table_catalog": "test_catalog",
        "table_schema": "test_schema",
        "table_name": "test_table",
        "table_type": "TABLE",
        "ordinal_position": 1,
        "data_type": "varchar",
        "is_nullable": "YES",
    }
