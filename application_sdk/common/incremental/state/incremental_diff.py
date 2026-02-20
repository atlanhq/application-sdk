"""Incremental diff generation utilities.

This module creates incremental-diff folders containing only changed assets
from the current extraction run. The incremental-diff is used for efficient
publishing of only what changed, rather than the full current state.

Key concepts:
- Tables: CREATED, UPDATED, or BACKFILL (not NO CHANGE)
- Columns: Only for CREATED/UPDATED/BACKFILL tables
- Schemas/Databases: All (they're small and provide context)

BACKFILL tables are detected by comparing current vs previous state:
tables that exist now but weren't in previous state (e.g., due to filter changes).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Set

from application_sdk.common.incremental.helpers import (
    copy_directory_parallel,
    count_json_files_recursive,
)
from application_sdk.common.incremental.models import (
    EntityType,
    IncrementalDiffResult,
    TableScope,
)
from application_sdk.common.incremental.state.table_scope import (
    get_table_state,
    iter_scope_table_qns,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnection,
    get_parent_table_qn_expr,
    json_scan,
    managed_duckdb_connection,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def create_incremental_diff(
    transformed_dir: Path,
    incremental_diff_dir: Path,
    table_scope: TableScope,
    previous_state_dir: Optional[Path] = None,
    conn: DuckDBConnection = None,
    copy_workers: int = 3,
    get_backfill_tables_fn: Optional[
        Callable[[Path, Optional[Path]], Optional[Set[str]]]
    ] = None,
) -> IncrementalDiffResult:
    """Create incremental-diff folder with only changed assets from this run.

    The incremental-diff contains assets that changed in this specific run:
    - Tables: CREATED, UPDATED, or BACKFILL (not NO CHANGE)
    - Columns: Only for CREATED/UPDATED/BACKFILL tables
    - Schemas/Databases: All (they're small and provide context)

    BACKFILL tables are detected by comparing current vs previous state:
    tables that exist now but weren't in previous state (e.g., due to filter changes).

    Args:
        transformed_dir: Path to current run's transformed output
        incremental_diff_dir: Path to write incremental diff
        table_scope: Current table scope with incremental states
        previous_state_dir: Path to previous run's current-state (for backfill detection)
        conn: Optional DuckDB connection to reuse. If None, creates a new connection.
        copy_workers: Number of parallel workers for file copy operations (default: 3)
        get_backfill_tables_fn: Optional function to detect backfill tables.
            Signature: (transformed_dir, previous_state_dir) -> Optional[Set[str]]
            If not provided, backfill detection is skipped.

    Returns:
        IncrementalDiffResult with counts of written entities
    """
    result = IncrementalDiffResult()
    incremental_diff_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 1: Identify changed tables (CREATED/UPDATED) and backfill tables
    # ------------------------------------------------------------------
    backfill_tables: Set[str] = set()
    if get_backfill_tables_fn is not None:
        backfill_tables = (
            get_backfill_tables_fn(transformed_dir, previous_state_dir) or set()
        )

    changed_tables: Set[str] = set()
    for qn in iter_scope_table_qns(table_scope):
        state = get_table_state(table_scope, qn)
        if state in ("CREATED", "UPDATED"):
            changed_tables.add(qn)

    backfill_only_tables = backfill_tables - changed_tables
    all_changed_tables = changed_tables | backfill_only_tables

    state_counts = table_scope.state_counts
    result.tables_created = state_counts.get("CREATED", 0)
    result.tables_updated = state_counts.get("UPDATED", 0)
    result.tables_backfill = len(backfill_only_tables)

    # ------------------------------------------------------------------
    # Step 2: Filter and write changed tables to incremental-diff
    # ------------------------------------------------------------------
    if all_changed_tables:
        table_dir = incremental_diff_dir.joinpath(EntityType.TABLE.value)
        table_dir.mkdir(parents=True, exist_ok=True)
        _filter_entities_by_qualified_names(
            source_dir=transformed_dir.joinpath(EntityType.TABLE.value),
            dest_dir=table_dir,
            valid_qualified_names=all_changed_tables,
            start_chunk_idx=0,
            conn=conn,
        )

    # ------------------------------------------------------------------
    # Step 3: Filter and write columns for changed tables only
    # ------------------------------------------------------------------
    if all_changed_tables:
        columns_count = _filter_columns_by_tables(
            source_dir=transformed_dir.joinpath(EntityType.COLUMN.value),
            dest_dir=incremental_diff_dir.joinpath(EntityType.COLUMN.value),
            valid_table_qns=all_changed_tables,
            conn=conn,
        )
        result.columns_total = columns_count or 0

    # ------------------------------------------------------------------
    # Step 4: Copy all schemas and databases (small, provide context)
    # ------------------------------------------------------------------
    for entity_type in [EntityType.SCHEMA, EntityType.DATABASE]:
        entity_dir = transformed_dir.joinpath(entity_type)
        if entity_dir.exists():
            dest_dir = incremental_diff_dir.joinpath(entity_type)
            count = copy_directory_parallel(
                entity_dir, dest_dir, max_workers=copy_workers
            )

            if entity_type == EntityType.SCHEMA:
                result.schemas_total = count
            elif entity_type == EntityType.DATABASE:
                result.databases_total = count

    result.total_files = count_json_files_recursive(incremental_diff_dir)

    logger.info(
        "Incremental-diff created: %d tables (created=%d, updated=%d, backfill=%d), "
        "%d columns, %d total files",
        result.tables_created + result.tables_updated + result.tables_backfill,
        result.tables_created,
        result.tables_updated,
        result.tables_backfill,
        result.columns_total,
        result.total_files,
    )

    return result


def _filter_entities_by_qualified_names(
    source_dir: Path,
    dest_dir: Path,
    valid_qualified_names: Set[str],
    start_chunk_idx: int = 0,
    conn: DuckDBConnection = None,
) -> Optional[int]:
    """Filter entities (tables, schemas, etc.) by qualified name using DuckDB COPY TO.

    Args:
        source_dir: Path to source entity JSON files
        dest_dir: Path to write filtered files
        valid_qualified_names: Set of qualified names to include
        start_chunk_idx: Starting chunk index for output files (default: 0)
        conn: Optional DuckDB connection to reuse. If None, creates a new connection.

    Returns:
        Count of entities written, or None if source doesn't exist or is empty
    """
    if not source_dir.exists():
        return None

    json_files = list(source_dir.glob("*.json"))
    if not json_files:
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)

    with managed_duckdb_connection(conn) as active_conn:
        # Unique DuckDB table names to avoid conflicts when connection is reused
        qualified_names_lookup = "diff_qualified_names_lookup"
        entities_data = "diff_entities_data"

        # Clean up any existing tables from previous runs
        active_conn.execute(f"DROP TABLE IF EXISTS {qualified_names_lookup}")
        active_conn.execute(f"DROP TABLE IF EXISTS {entities_data}")

        # ------------------------------------------------------------------
        # Step 1: Create lookup table of valid qualified names
        # ------------------------------------------------------------------
        active_conn.execute(
            f"CREATE TABLE {qualified_names_lookup} (qualified_name VARCHAR)"
        )
        active_conn.executemany(
            f"INSERT INTO {qualified_names_lookup} VALUES (?)",
            [(qn,) for qn in valid_qualified_names],
        )

        # ------------------------------------------------------------------
        # Step 2: Load entities from JSON files
        # ------------------------------------------------------------------
        active_conn.execute(f"""
            CREATE TABLE {entities_data} AS
            SELECT *
            FROM {json_scan(json_files)}
            WHERE attributes.qualifiedName IS NOT NULL
        """)

        # ------------------------------------------------------------------
        # Step 3: Count and write matching entities
        # ------------------------------------------------------------------
        count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {entities_data} e
            JOIN {qualified_names_lookup} qn_lookup
              ON e.attributes.qualifiedName = qn_lookup.qualified_name
        """).fetchone()

        count = count_result[0] if count_result else 0

        if count == 0:
            return None

        dest_file = dest_dir.joinpath(f"chunk-{start_chunk_idx}.json")

        dest_file_str = str(dest_file).replace("'", "''")
        active_conn.execute(
            f"""
            COPY (
                SELECT e.*
                FROM {entities_data} e
                JOIN {qualified_names_lookup} qn_lookup
                  ON e.attributes.qualifiedName = qn_lookup.qualified_name
            )
            TO '{dest_file_str}'
            (FORMAT JSON, ARRAY false)
        """
        )

        return count


def _filter_columns_by_tables(
    source_dir: Path,
    dest_dir: Path,
    valid_table_qns: Set[str],
    conn: DuckDBConnection = None,
) -> Optional[int]:
    """Filter columns by parent table using DuckDB COPY TO.

    Args:
        source_dir: Path to source column JSON files
        dest_dir: Path to write filtered files
        valid_table_qns: Set of table qualified names to include columns for
        conn: Optional DuckDB connection to reuse. If None, creates a new connection.

    Returns:
        Count of columns written, or None if source doesn't exist or is empty
    """
    if not source_dir.exists():
        return None

    json_files = list(source_dir.glob("*.json"))
    if not json_files:
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)

    with managed_duckdb_connection(conn) as active_conn:
        # Unique DuckDB table names to avoid conflicts when connection is reused
        parent_tables_lookup = "diff_parent_tables_lookup"
        columns_data = "diff_columns_data"

        # Clean up any existing tables from previous runs
        active_conn.execute(f"DROP TABLE IF EXISTS {parent_tables_lookup}")
        active_conn.execute(f"DROP TABLE IF EXISTS {columns_data}")

        # ------------------------------------------------------------------
        # Step 1: Create lookup table of valid parent table QNs
        # ------------------------------------------------------------------
        active_conn.execute(f"CREATE TABLE {parent_tables_lookup} (table_qn VARCHAR)")
        active_conn.executemany(
            f"INSERT INTO {parent_tables_lookup} VALUES (?)",
            [(qn,) for qn in valid_table_qns],
        )

        # ------------------------------------------------------------------
        # Step 2: Load columns with derived parent table QN
        # ------------------------------------------------------------------
        json_scan_sql = json_scan(json_files)
        table_qn_expr = get_parent_table_qn_expr()

        active_conn.execute(f"""
            CREATE TABLE {columns_data} AS
            SELECT
                *,
                {table_qn_expr} AS parent_table_qn
            FROM {json_scan_sql}
            WHERE {table_qn_expr} IS NOT NULL
        """)

        # ------------------------------------------------------------------
        # Step 3: Count and write matching columns
        # ------------------------------------------------------------------
        count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {columns_data} c
            JOIN {parent_tables_lookup} pt_lookup ON c.parent_table_qn = pt_lookup.table_qn
        """).fetchone()

        count = count_result[0] if count_result else 0

        if count == 0:
            return None

        dest_file = dest_dir.joinpath("chunk-0.json")
        dest_file_str = str(dest_file).replace("'", "''")
        active_conn.execute(
            f"""
            COPY (
                SELECT c.* EXCLUDE (parent_table_qn)
                FROM {columns_data} c
                JOIN {parent_tables_lookup} pt_lookup
                  ON c.parent_table_qn = pt_lookup.table_qn
            )
            TO '{dest_file_str}'
            (FORMAT JSON, ARRAY false)
        """
        )

        return count
