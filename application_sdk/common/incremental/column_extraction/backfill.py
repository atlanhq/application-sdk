"""DuckDB-based backfill detection for incremental column extraction.

This module compares current transformed tables with previous current-state
to detect tables that need backfilling (exist now but were not in the
previous state, e.g., due to filter changes).

Functions:
    get_backfill_tables: Compare current vs previous state using DuckDB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

import duckdb

from application_sdk.common.incremental.models import EntityType
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def get_backfill_tables(
    current_transformed_dir: Path, previous_current_state_dir: Path | None
) -> Set[str] | None:
    """Use DuckDB to compare current tables vs previous current-state.

    Returns qualified names of tables that need backfilling
    (exist now but not in previous state).

    Args:
        current_transformed_dir: Path to current run's transformed output.
        previous_current_state_dir: Path to previous run's current-state directory.

    Returns:
        Set of qualified names needing backfill, or None if not applicable.
    """
    if not previous_current_state_dir or not previous_current_state_dir.exists():
        logger.info("No previous state available - returning None (should be full run)")
        return None

    try:
        with DuckDBConnectionManager() as conn_manager:
            conn = conn_manager.connection

            current_tables = _load_tables_to_duckdb(
                conn, current_transformed_dir, "current_tables"
            )

            previous_tables = _load_tables_to_duckdb(
                conn, previous_current_state_dir, "previous_tables"
            )

            if current_tables is None or current_tables == 0:
                raise ValueError(
                    "No transformed tables found for finding backfill tables"
                )

            if previous_tables is None or previous_tables == 0:
                logger.warning(
                    "Previous state directory exists but contains no table files. "
                    "Cannot determine backfill tables without valid previous state."
                )
                return None

            # Find tables that exist in current but not in previous
            result = conn.execute("""
                SELECT DISTINCT c.qualified_name
                FROM current_tables c
                LEFT JOIN previous_tables p
                    ON c.qualified_name = p.qualified_name
                    AND c.type_name = p.type_name
                WHERE p.qualified_name IS NULL
            """).fetchall()

            backfill_qns = {row[0] for row in result}

            if backfill_qns:
                logger.info(f"Found {len(backfill_qns)} assets needing backfill:")
                for qn in sorted(backfill_qns):
                    type_result = conn.execute(
                        """
                        SELECT type_name FROM current_tables
                        WHERE qualified_name = ? LIMIT 1
                    """,
                        [qn],
                    ).fetchone()
                    asset_type = type_result[0] if type_result else None
                    logger.info(f"  - {asset_type}: {qn}")
            else:
                logger.info(
                    "No backfill tables found - all current tables exist in "
                    "previous state"
                )

            return backfill_qns

    except Exception as e:
        logger.error(f"DuckDB analysis failed: {e}, returning None")
        return None


def _load_tables_to_duckdb(
    conn: duckdb.DuckDBPyConnection, base_dir: Path, table_name: str
) -> int | None:
    """Load table/view JSON files into DuckDB using read_json_auto.

    Uses DuckDB's native JSON loading - no Python memory loading required.

    Args:
        conn: DuckDB connection instance.
        base_dir: Base directory containing table subdirectory with JSON files.
        table_name: Name for the DuckDB table to create.

    Returns:
        Number of records loaded, or None if no JSON files found.
    """
    try:
        table_dir = base_dir.joinpath(EntityType.TABLE.value)
        json_files = []
        if table_dir.exists():
            json_files = [f.resolve() for f in table_dir.glob("*.json")]
    except OSError as e:
        logger.error(f"Failed to scan JSON files from '{base_dir}': {e}")
        raise

    if not json_files:
        conn.execute(f"""
            CREATE TABLE {table_name} (
                type_name VARCHAR,
                qualified_name VARCHAR
            )
        """)
        return None

    # Escape single quotes for SQL
    escaped_files = [str(f.resolve()).replace("'", "''") for f in json_files]

    # Build UNION ALL query for multiple files
    union_parts = []
    for escaped_file in escaped_files:
        union_parts.append(f"""
            SELECT
                typeName AS type_name,
                attributes.qualifiedName AS qualified_name
            FROM read_json_auto('{escaped_file}',
                format='newline_delimited',
                ignore_errors=true
            )
            WHERE typeName IS NOT NULL
              AND attributes.qualifiedName IS NOT NULL
        """)

    union_query = " UNION ALL ".join(union_parts)

    try:
        conn.execute(f"""
            CREATE TABLE {table_name} AS
            {union_query}
        """)
    except duckdb.Error as e:
        logger.error(
            f"DuckDB failed to load {len(json_files)} JSON files "
            f"into '{table_name}': {e}"
        )
        raise

    result = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return result[0] if result else None
