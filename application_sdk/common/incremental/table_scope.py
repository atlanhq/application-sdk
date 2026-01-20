"""Table scope utilities for incremental extraction.

This module provides utilities for detecting tables under include/exclude filters
and their incremental states from transformed output directories.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Set

from application_sdk.observability.logger_adaptor import get_logger

from application_sdk.common.incremental.constants import INCREMENTAL_DEFAULT_STATE
from application_sdk.common.incremental.models import EntityType, TableScope
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnection,
    managed_duckdb_connection,
    json_scan,
    get_parent_table_qn_expr,
    escape_sql_string,
)
from application_sdk.common.incremental.storage.rocksdb_utils import close_states_db

logger = get_logger(__name__)


# =============================================================================
# TableScope Helper Functions
# =============================================================================


def add_table_to_scope(scope: TableScope, qualified_name: str, state: str) -> None:
    """Add a table to the scope with its incremental state."""
    scope.table_qualified_names.add(qualified_name)
    scope.table_states[qualified_name] = state


def get_table_state(scope: TableScope, qualified_name: str) -> str | None:
    """Get the incremental state for a table."""
    if not scope.table_states.key_may_exist(qualified_name):
        return None
    return scope.table_states.get(qualified_name)


def get_scope_length(scope: TableScope) -> int:
    """Return the number of tables in scope."""
    return len(scope.table_qualified_names)


def iter_scope_table_qns(scope: TableScope) -> Iterator[str]:
    """Iterate over all table qualified names in scope."""
    return iter(scope.table_qualified_names)


def close_scope(scope: TableScope) -> None:
    """Close RocksDB table_states and cleanup its temporary directory."""
    close_states_db(scope.table_states)


# =============================================================================
# Main Functions
# =============================================================================


def get_current_table_scope(
    transformed_dir: Path,
    conn: DuckDBConnection = None,
) -> TableScope | None:
    """Get current table scope within the current run's include/exclude filters.

    Args:
        transformed_dir: Path to current run's transformed output directory
        conn: Optional DuckDB connection to reuse

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

            result = active_conn.execute(
                f"SELECT qualified_name, incremental_state FROM {tables_with_states_data}"
            ).fetchall()

            for qn, state in result:
                add_table_to_scope(scope, qn, state)

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
) -> Set[str] | None:
    """Extract table qualified names from column files in the given directory.

    Args:
        column_dir: Path to directory containing column JSON files
        conn: Optional DuckDB connection to reuse

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
                "No valid table references found. Column dir: %s, files: %d, rows: %d",
                column_dir,
                len(json_files),
                row_count,
            )
            return None

    except Exception as e:
        logger.error(
            "Failed to get tables with columns from %s: %s", column_dir, e, exc_info=True
        )
        return None
