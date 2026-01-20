"""Incremental diff generation for changed assets.

This module creates incremental-diff folders with only changed assets
from the current extraction run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

from application_sdk.observability.logger_adaptor import get_logger

from application_sdk.common.incremental.models import (
    EntityType,
    TableScope,
    IncrementalDiffResult,
)
from application_sdk.common.incremental.helpers import (
    get_backfill_tables,
    count_json_files_recursive,
    copy_directory_parallel,
)
from application_sdk.common.incremental.table_scope import (
    iter_scope_table_qns,
    get_table_state,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnection,
    managed_duckdb_connection,
    json_scan,
    get_parent_table_qn_expr,
)

logger = get_logger(__name__)


def create_incremental_diff(
    transformed_dir: Path,
    incremental_diff_dir: Path,
    table_scope: TableScope,
    previous_state_dir: Path | None = None,
    conn: DuckDBConnection = None,
    copy_workers: int = 3,
) -> IncrementalDiffResult:
    """Create incremental-diff folder with only changed assets from this run.

    The incremental-diff contains assets that changed in this specific run:
    - Tables: CREATED, UPDATED, or BACKFILL (not NO CHANGE)
    - Columns: Only for CREATED/UPDATED/BACKFILL tables
    - Schemas/Databases: All (small, provide context)

    Args:
        transformed_dir: Path to current run's transformed output
        incremental_diff_dir: Path to write incremental diff
        table_scope: Current table scope with incremental states
        previous_state_dir: Path to previous run's current-state
        conn: Optional DuckDB connection to reuse
        copy_workers: Number of parallel workers for file copy

    Returns:
        IncrementalDiffResult with counts of written entities
    """
    result = IncrementalDiffResult()
    incremental_diff_dir.mkdir(parents=True, exist_ok=True)

    # Identify changed and backfill tables
    backfill_tables = (
        get_backfill_tables(
            current_transformed_dir=transformed_dir,
            previous_current_state_dir=previous_state_dir,
        )
        or set()
    )

    changed_tables = set()
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

    # Filter and write changed tables
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

    # Filter and write columns for changed tables
    if all_changed_tables:
        columns_count = _filter_columns_by_tables(
            source_dir=transformed_dir.joinpath(EntityType.COLUMN.value),
            dest_dir=incremental_diff_dir.joinpath(EntityType.COLUMN.value),
            valid_table_qns=all_changed_tables,
            conn=conn,
        )
        result.columns_total = columns_count or 0

    # Copy schemas and databases
    for entity_type in [EntityType.SCHEMA, EntityType.DATABASE]:
        entity_dir = transformed_dir.joinpath(entity_type.value)
        if entity_dir.exists():
            dest_dir = incremental_diff_dir.joinpath(entity_type.value)
            count = copy_directory_parallel(entity_dir, dest_dir, max_workers=copy_workers)

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


def _get_next_chunk_index(dest_dir: Path) -> int:
    """Get the next available chunk index for output files."""
    if not dest_dir.exists():
        return 0

    existing_files = list(dest_dir.glob("chunk-*.json"))
    if not existing_files:
        return 0

    indices = []
    for f in existing_files:
        try:
            idx = int(f.stem.split("-")[-1])
            indices.append(idx)
        except (ValueError, IndexError):
            pass

    return max(indices) + 1 if indices else 0


def _filter_entities_by_qualified_names(
    source_dir: Path,
    dest_dir: Path,
    valid_qualified_names: Set[str],
    start_chunk_idx: int | None = None,
    conn: DuckDBConnection = None,
) -> int | None:
    """Filter entities by qualified name using DuckDB COPY TO."""
    if not source_dir.exists():
        return None

    json_files = list(source_dir.glob("*.json"))
    if not json_files:
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)

    with managed_duckdb_connection(conn) as active_conn:
        qualified_names_lookup = "diff_qualified_names_lookup"
        entities_data = "diff_entities_data"

        active_conn.execute(f"DROP TABLE IF EXISTS {qualified_names_lookup}")
        active_conn.execute(f"DROP TABLE IF EXISTS {entities_data}")

        active_conn.execute(
            f"CREATE TABLE {qualified_names_lookup} (qualified_name VARCHAR)"
        )
        active_conn.executemany(
            f"INSERT INTO {qualified_names_lookup} VALUES (?)",
            [(qn,) for qn in valid_qualified_names],
        )

        active_conn.execute(f"""
            CREATE TABLE {entities_data} AS
            SELECT *
            FROM {json_scan(json_files)}
            WHERE attributes.qualifiedName IS NOT NULL
        """)

        count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {entities_data} e
            JOIN {qualified_names_lookup} qn_lookup 
                ON e.attributes.qualifiedName = qn_lookup.qualified_name
        """).fetchone()

        count = count_result[0] if count_result else 0

        if count == 0:
            return None

        chunk_idx = (
            start_chunk_idx
            if start_chunk_idx is not None
            else _get_next_chunk_index(dest_dir)
        )
        dest_file = dest_dir.joinpath(f"chunk-{chunk_idx}.json")

        active_conn.execute(f"""
            COPY (
                SELECT e.*
                FROM {entities_data} e
                JOIN {qualified_names_lookup} qn_lookup 
                    ON e.attributes.qualifiedName = qn_lookup.qualified_name
            )
            TO ?
            (FORMAT JSON, ARRAY false)
        """, [str(dest_file)])

        return count


def _filter_columns_by_tables(
    source_dir: Path,
    dest_dir: Path,
    valid_table_qns: Set[str],
    conn: DuckDBConnection = None,
) -> int | None:
    """Filter columns by parent table using DuckDB COPY TO."""
    if not source_dir.exists():
        return None

    json_files = list(source_dir.glob("*.json"))
    if not json_files:
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)

    with managed_duckdb_connection(conn) as active_conn:
        parent_tables_lookup = "diff_parent_tables_lookup"
        columns_data = "diff_columns_data"

        active_conn.execute(f"DROP TABLE IF EXISTS {parent_tables_lookup}")
        active_conn.execute(f"DROP TABLE IF EXISTS {columns_data}")

        active_conn.execute(
            f"CREATE TABLE {parent_tables_lookup} (table_qn VARCHAR)"
        )
        active_conn.executemany(
            f"INSERT INTO {parent_tables_lookup} VALUES (?)",
            [(qn,) for qn in valid_table_qns],
        )

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

        count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {columns_data} c
            JOIN {parent_tables_lookup} pt_lookup 
                ON c.parent_table_qn = pt_lookup.table_qn
        """).fetchone()

        count = count_result[0] if count_result else 0

        if count == 0:
            return None

        dest_file = dest_dir.joinpath("chunk-0.json")
        active_conn.execute(f"""
            COPY (
                SELECT c.* EXCLUDE (parent_table_qn)
                FROM {columns_data} c
                JOIN {parent_tables_lookup} pt_lookup 
                    ON c.parent_table_qn = pt_lookup.table_qn
            )
            TO ?
            (FORMAT JSON, ARRAY false)
        """, [str(dest_file)])

        return count
