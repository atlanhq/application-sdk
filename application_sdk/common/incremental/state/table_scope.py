"""Table scope and state detection utilities.

This module provides utilities for detecting tables under include/exclude filters
and incremental states from transformed output directories.

Key functions:
- get_current_table_scope: Extract table qualified names and their incremental states
- get_table_qns_from_columns: Extract table qualified names from column files

TableScope helper functions:
- add_table_to_scope: Add a table with its state
- get_table_state: Get incremental state for a table
- get_scope_length: Get number of tables in scope
- iter_scope_table_qns: Iterate over all table qualified names
- close_scope: Close and release resources
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional, Set

from application_sdk.common.incremental.models import EntityType, TableScope
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnection,
    escape_sql_string,
    get_parent_table_qn_expr,
    json_scan,
    managed_duckdb_connection,
)
from application_sdk.common.incremental.storage.rocksdb_utils import close_states_db
from application_sdk.constants import INCREMENTAL_DEFAULT_STATE
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# =============================================================================
# TableScope Helper Functions
# =============================================================================


def add_table_to_scope(scope: TableScope, qualified_name: str, state: str) -> None:
    """Add a table to the scope with its incremental state.

    Args:
        scope: TableScope instance to modify
        qualified_name: Table qualified name
        state: Incremental state (CREATED, UPDATED, NO CHANGE)
    """
    scope.table_qualified_names.add(qualified_name)
    scope.table_states[qualified_name] = state


def get_table_state(scope: TableScope, qualified_name: str) -> Optional[str]:
    """Get the incremental state for a table.

    Uses RocksDB's key_may_exist() for fast negative lookups when available.

    Args:
        scope: TableScope instance
        qualified_name: Table qualified name

    Returns:
        Incremental state or None if not found
    """
    # Fast path for missing keys (if RocksDB supports it)
    if hasattr(scope.table_states, "key_may_exist"):
        if not scope.table_states.key_may_exist(qualified_name):
            return None
    return scope.table_states.get(qualified_name)


def get_scope_length(scope: TableScope) -> int:
    """Return the number of tables in scope.

    Args:
        scope: TableScope instance

    Returns:
        Number of tables
    """
    return len(scope.table_qualified_names)


def iter_scope_table_qns(scope: TableScope) -> Iterator[str]:
    """Iterate over all table qualified names in scope.

    Args:
        scope: TableScope instance

    Yields:
        Table qualified names
    """
    return iter(scope.table_qualified_names)


def close_scope(scope: TableScope) -> None:
    """Close RocksDB table_states and cleanup its temporary directory.

    Call this explicitly when done with TableScope to ensure proper cleanup.

    Args:
        scope: TableScope instance to close
    """
    close_states_db(scope.table_states)


# =============================================================================
# Main Functions
# =============================================================================


def get_current_table_scope(
    transformed_dir: Path,
    conn: DuckDBConnection = None,
) -> Optional[TableScope]:
    """Get current table scope within the current run's include/exclude filters.

    Args:
        transformed_dir: Path to current run's transformed output directory
        conn: Optional DuckDB connection to reuse. If None, creates a new connection.

    Returns:
        TableScope with table qualified names and their incremental states,
        or None if table directory doesn't exist or contains no JSON files
    """
    table_dir = transformed_dir.joinpath(EntityType.TABLE.value)
    if not table_dir.exists():
        logger.warning("Table directory not found: %s", table_dir)
        return None

    json_files = list(table_dir.glob("*.json"))
    if not json_files:
        logger.warning("No JSON files found in: %s", table_dir)
        return None

    scope = TableScope()

    try:
        with managed_duckdb_connection(conn) as active_conn:
            # ------------------------------------------------------------------
            # Step 1: Load tables from JSON with qualified names and states
            # ------------------------------------------------------------------
            incremental_default = escape_sql_string(INCREMENTAL_DEFAULT_STATE)
            tables_with_states_data = "scope_tables_with_states_data"
            active_conn.execute(f"DROP TABLE IF EXISTS {tables_with_states_data}")
            active_conn.execute(f"""
                CREATE TABLE {tables_with_states_data} AS
                SELECT
                    json_extract_string(to_json(attributes), 'qualifiedName') AS qualified_name,
                    COALESCE(
                        json_extract_string(customAttributes, '$.incremental_state'),
                        '{incremental_default}'
                    ) AS incremental_state
                FROM {json_scan(json_files)}
            """)

            active_conn.execute(
                f"DELETE FROM {tables_with_states_data} WHERE qualified_name IS NULL"
            )

            # ------------------------------------------------------------------
            # Step 2: Populate TableScope
            # ------------------------------------------------------------------
            result = active_conn.execute(
                f"SELECT qualified_name, incremental_state FROM {tables_with_states_data}"
            ).fetchall()

            for qn, state in result:
                add_table_to_scope(scope, qn, state)

            # ------------------------------------------------------------------
            # Step 3: Cache state counts for efficient access
            # ------------------------------------------------------------------
            state_counts = active_conn.execute(f"""
                SELECT incremental_state, COUNT(*) as cnt
                FROM (SELECT DISTINCT qualified_name, incremental_state FROM {tables_with_states_data})
                GROUP BY incremental_state
            """).fetchall()

            state_map = {state: cnt for state, cnt in state_counts}
            scope.state_counts = state_map

            logger.info(
                "Table scope loaded: %d tables (CREATED=%d, UPDATED=%d, NO CHANGE=%d)",
                get_scope_length(scope),
                state_map.get("CREATED", 0),
                state_map.get("UPDATED", 0),
                state_map.get("NO CHANGE", 0),
            )

    except Exception as e:
        logger.error("Failed to load table scope: %s", e)
        close_scope(scope)
        raise

    return scope


def get_table_qns_from_columns(
    column_dir: Path,
    conn: DuckDBConnection = None,
) -> Optional[Set[str]]:
    """Extract table qualified names from column files in the given directory.

    Args:
        column_dir: Path to directory containing column JSON files
        conn: Optional DuckDB connection to reuse. If None, creates a new connection.

    Returns:
        Set of table qualified names that have columns,
        or None if directory doesn't exist or contains no JSON files
    """
    if not column_dir.exists():
        logger.warning("Column directory does not exist: %s", column_dir)
        return None

    json_files = list(column_dir.glob("*.json"))
    if not json_files:
        logger.warning("No JSON files found in column directory: %s", column_dir)
        return None

    try:
        with managed_duckdb_connection(conn) as active_conn:
            json_scan_sql = json_scan(json_files)
            table_qn_expr = get_parent_table_qn_expr()

            result = active_conn.execute(f"""
                SELECT DISTINCT {table_qn_expr} AS table_qn
                FROM {json_scan_sql}
                WHERE {table_qn_expr} IS NOT NULL
            """).fetchall()

            tables = {row[0] for row in result}

            if tables:
                logger.info("Found %d tables with columns", len(tables))
                return tables

            total_rows = active_conn.execute(
                f"SELECT COUNT(*) FROM {json_scan_sql}"
            ).fetchone()
            row_count = total_rows[0] if total_rows else 0
            logger.warning(
                "No valid tableQualifiedName/viewQualifiedName found. "
                "Column dir: %s, files: %d, rows: %d",
                column_dir,
                len(json_files),
                row_count,
            )
            return None

    except Exception as e:
        logger.error(
            "Failed to get tables with columns from %s: %s",
            column_dir,
            e,
            exc_info=True,
        )
        return None
