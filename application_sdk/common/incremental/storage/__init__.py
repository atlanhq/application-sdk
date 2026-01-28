"""Storage utilities for incremental extraction.

This module provides:
- DuckDB utilities for efficient SQL-based JSON processing
- RocksDB utilities for disk-backed state storage
"""

from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnection,
    DuckDBConnectionManager,
    managed_duckdb_connection,
    escape_sql_string,
    json_scan,
    get_parent_table_qn_expr,
)
from application_sdk.common.incremental.storage.rocksdb_utils import (
    create_states_db,
    close_states_db,
)

__all__ = [
    # DuckDB
    "DuckDBConnection",
    "DuckDBConnectionManager",
    "managed_duckdb_connection",
    "escape_sql_string",
    "json_scan",
    "get_parent_table_qn_expr",
    # RocksDB
    "create_states_db",
    "close_states_db",
]
