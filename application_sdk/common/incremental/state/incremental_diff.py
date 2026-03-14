"""Incremental diff generation utilities.

This module creates incremental-diff folders containing only changed assets
from the current extraction run. The incremental-diff is used for efficient
publishing of only what changed, rather than the full current state.

Key concepts:
- Tables: CREATED, UPDATED, BACKFILL, or DELETED
- Columns: For CREATED/UPDATED/BACKFILL tables + deleted columns
- Schemas/Databases: All (they're small and provide context)
- Deletions: Tables in previous state but not in current, plus cascade to columns

BACKFILL tables are detected by comparing current vs previous state:
tables that exist now but weren't in previous state (e.g., due to filter changes).

Deletion detection:
- Table-level: Tables in previous state but not in current extraction scope
- Column-level for deleted tables: All columns cascade-deleted
- Column-level for UPDATED tables: Columns in previous but not in current

Output structure:
    incremental-diff/{run_id}/
        table/          - CREATED/UPDATED/BACKFILL tables
        column/         - Columns for changed tables
        schema/         - All schemas
        database/       - All databases
        delete/table/   - Deleted table entities
        delete/column/  - Deleted column entities
        metadata.json   - Entity counts for Argo routing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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
    """Create incremental-diff folder with changed and deleted assets from this run.

    The incremental-diff contains assets that changed in this specific run:
    - Tables: CREATED, UPDATED, or BACKFILL (not NO CHANGE)
    - Columns: Only for CREATED/UPDATED/BACKFILL tables
    - Schemas/Databases: All (they're small and provide context)
    - Deleted tables: Tables in previous state but not in current scope
    - Deleted columns: Cascade from deleted tables + missing from UPDATED tables

    Args:
        transformed_dir: Path to current run's transformed output
        incremental_diff_dir: Path to write incremental diff
        table_scope: Current table scope with incremental states
        previous_state_dir: Path to previous run's current-state (for backfill/deletion)
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
    updated_tables: Set[str] = set()
    for qn in iter_scope_table_qns(table_scope):
        state = get_table_state(table_scope, qn)
        if state in ("CREATED", "UPDATED"):
            changed_tables.add(qn)
        if state == "UPDATED":
            updated_tables.add(qn)

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

    # ------------------------------------------------------------------
    # Step 5: Detect deleted tables and cascade to columns
    # ------------------------------------------------------------------
    current_table_qns = set(iter_scope_table_qns(table_scope))

    if previous_state_dir and previous_state_dir.exists():
        deletion_counts = _detect_deletions(
            previous_state_dir=previous_state_dir,
            current_table_qns=current_table_qns,
            updated_table_qns=updated_tables,
            current_column_dir=transformed_dir.joinpath(EntityType.COLUMN.value),
            delete_dir=incremental_diff_dir.joinpath("delete"),
            conn=conn,
        )
        result.tables_deleted = deletion_counts.get("tables_deleted", 0)
        result.columns_deleted = deletion_counts.get("columns_deleted", 0)

    # ------------------------------------------------------------------
    # Step 6: Count total files and write metadata.json
    # ------------------------------------------------------------------
    result.total_files = count_json_files_recursive(incremental_diff_dir)

    _write_metadata(incremental_diff_dir, result)

    logger.info(
        "Incremental-diff created: %d tables (created=%d, updated=%d, "
        "backfill=%d, deleted=%d), %d columns, %d deleted columns, "
        "%d total files, %d total changed entities",
        result.tables_created + result.tables_updated + result.tables_backfill,
        result.tables_created,
        result.tables_updated,
        result.tables_backfill,
        result.tables_deleted,
        result.columns_total,
        result.columns_deleted,
        result.total_files,
        result.total_changed_entities,
    )

    return result


def _detect_deletions(
    previous_state_dir: Path,
    current_table_qns: Set[str],
    updated_table_qns: Set[str],
    current_column_dir: Path,
    delete_dir: Path,
    conn: DuckDBConnection = None,
) -> Dict[str, int]:
    """Detect deleted tables and columns by comparing previous vs current state.

    Deletion detection covers two scenarios:
    1. Table-level: Tables in previous state but not in current scope.
       Their columns are cascade-deleted.
    2. Column-level for UPDATED tables: Columns in previous state but
       not in current extraction for tables that were re-extracted.

    Args:
        previous_state_dir: Path to previous run's current-state
        current_table_qns: Set of table QNs in the current extraction
        updated_table_qns: Set of table QNs with UPDATED state
        current_column_dir: Path to current run's transformed column directory
        delete_dir: Path to write delete records (delete/table/, delete/column/)
        conn: Optional DuckDB connection to reuse.

    Returns:
        Dictionary with tables_deleted and columns_deleted counts
    """
    counts: Dict[str, int] = {"tables_deleted": 0, "columns_deleted": 0}

    prev_table_dir = previous_state_dir.joinpath(EntityType.TABLE.value)
    if not prev_table_dir.exists():
        return counts

    prev_table_files = list(prev_table_dir.glob("*.json"))
    if not prev_table_files:
        return counts

    with managed_duckdb_connection(conn) as active_conn:
        # ------------------------------------------------------------------
        # Load previous state tables
        # ------------------------------------------------------------------
        del_prev_tables = "del_prev_tables"
        del_current_qns = "del_current_qns"

        active_conn.execute(f"DROP TABLE IF EXISTS {del_prev_tables}")
        active_conn.execute(f"DROP TABLE IF EXISTS {del_current_qns}")

        active_conn.execute(f"""
            CREATE TABLE {del_prev_tables} AS
            SELECT *
            FROM {json_scan(prev_table_files)}
            WHERE attributes.qualifiedName IS NOT NULL
        """)

        # Create lookup of current table QNs
        active_conn.execute(f"CREATE TABLE {del_current_qns} (qualified_name VARCHAR)")
        if current_table_qns:
            active_conn.executemany(
                f"INSERT INTO {del_current_qns} VALUES (?)",
                [(qn,) for qn in current_table_qns],
            )

        # ------------------------------------------------------------------
        # Step A: Find deleted tables (in previous but not in current)
        # ------------------------------------------------------------------
        deleted_count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {del_prev_tables} pt
            LEFT JOIN {del_current_qns} cq
              ON pt.attributes.qualifiedName = cq.qualified_name
            WHERE cq.qualified_name IS NULL
        """).fetchone()

        deleted_table_count = deleted_count_result[0] if deleted_count_result else 0

        if deleted_table_count > 0:
            delete_table_dir = delete_dir.joinpath(EntityType.TABLE.value)
            delete_table_dir.mkdir(parents=True, exist_ok=True)

            dest_file = delete_table_dir.joinpath("chunk-0.json")
            dest_file_str = str(dest_file).replace("'", "''")

            active_conn.execute(f"""
                COPY (
                    SELECT pt.*
                    FROM {del_prev_tables} pt
                    LEFT JOIN {del_current_qns} cq
                      ON pt.attributes.qualifiedName = cq.qualified_name
                    WHERE cq.qualified_name IS NULL
                )
                TO '{dest_file_str}'
                (FORMAT JSON, ARRAY false)
            """)

            counts["tables_deleted"] = deleted_table_count
            logger.info("Detected %d deleted tables", deleted_table_count)

            # Get the deleted table QNs for column cascade
            deleted_table_qns_result = active_conn.execute(f"""
                SELECT pt.attributes.qualifiedName
                FROM {del_prev_tables} pt
                LEFT JOIN {del_current_qns} cq
                  ON pt.attributes.qualifiedName = cq.qualified_name
                WHERE cq.qualified_name IS NULL
            """).fetchall()
            deleted_table_qns = {row[0] for row in deleted_table_qns_result}

            # Cascade: delete columns belonging to deleted tables
            cascade_count = _detect_deleted_columns_for_tables(
                previous_state_dir=previous_state_dir,
                table_qns=deleted_table_qns,
                delete_column_dir=delete_dir.joinpath(EntityType.COLUMN.value),
                chunk_idx=0,
                conn=active_conn,
            )
            counts["columns_deleted"] += cascade_count

        # ------------------------------------------------------------------
        # Step B: Find deleted columns for UPDATED tables
        # ------------------------------------------------------------------
        if updated_table_qns:
            updated_deleted = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=previous_state_dir,
                current_column_dir=current_column_dir,
                updated_table_qns=updated_table_qns,
                delete_column_dir=delete_dir.joinpath(EntityType.COLUMN.value),
                chunk_idx=1,
                conn=active_conn,
            )
            counts["columns_deleted"] += updated_deleted

    return counts


def _detect_deleted_columns_for_tables(
    previous_state_dir: Path,
    table_qns: Set[str],
    delete_column_dir: Path,
    chunk_idx: int = 0,
    conn: DuckDBConnection = None,
) -> int:
    """Write delete records for all columns belonging to specified tables.

    Used for cascade deletion when tables are removed.

    Args:
        previous_state_dir: Path to previous run's current-state
        table_qns: Set of table QNs whose columns should be deleted
        delete_column_dir: Path to write deleted column records
        chunk_idx: Starting chunk index for output files
        conn: Optional DuckDB connection to reuse.

    Returns:
        Number of deleted column records written
    """
    if not table_qns:
        return 0

    prev_col_dir = previous_state_dir.joinpath(EntityType.COLUMN.value)
    if not prev_col_dir.exists():
        return 0

    prev_col_files = list(prev_col_dir.glob("*.json"))
    if not prev_col_files:
        return 0

    with managed_duckdb_connection(conn) as active_conn:
        del_cascade_cols = "del_cascade_cols"
        del_cascade_tables = "del_cascade_tables"

        active_conn.execute(f"DROP TABLE IF EXISTS {del_cascade_cols}")
        active_conn.execute(f"DROP TABLE IF EXISTS {del_cascade_tables}")

        table_qn_expr = get_parent_table_qn_expr()

        active_conn.execute(f"""
            CREATE TABLE {del_cascade_cols} AS
            SELECT *, {table_qn_expr} AS parent_table_qn
            FROM {json_scan(prev_col_files)}
            WHERE {table_qn_expr} IS NOT NULL
        """)

        active_conn.execute(f"CREATE TABLE {del_cascade_tables} (table_qn VARCHAR)")
        active_conn.executemany(
            f"INSERT INTO {del_cascade_tables} VALUES (?)",
            [(qn,) for qn in table_qns],
        )

        count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {del_cascade_cols} c
            JOIN {del_cascade_tables} dt ON c.parent_table_qn = dt.table_qn
        """).fetchone()

        count = count_result[0] if count_result else 0
        if count == 0:
            return 0

        delete_column_dir.mkdir(parents=True, exist_ok=True)
        dest_file = delete_column_dir.joinpath(f"chunk-{chunk_idx}.json")
        dest_file_str = str(dest_file).replace("'", "''")

        active_conn.execute(f"""
            COPY (
                SELECT c.* EXCLUDE (parent_table_qn)
                FROM {del_cascade_cols} c
                JOIN {del_cascade_tables} dt ON c.parent_table_qn = dt.table_qn
            )
            TO '{dest_file_str}'
            (FORMAT JSON, ARRAY false)
        """)

        logger.info(
            "Cascade-deleted %d columns from %d deleted tables",
            count,
            len(table_qns),
        )
        return count


def _detect_deleted_columns_for_updated_tables(
    previous_state_dir: Path,
    current_column_dir: Path,
    updated_table_qns: Set[str],
    delete_column_dir: Path,
    chunk_idx: int = 1,
    conn: DuckDBConnection = None,
) -> int:
    """Detect columns deleted from UPDATED tables.

    Compares previous state columns vs current columns for UPDATED tables.
    Columns present in previous but absent in current are written as deletes.

    Args:
        previous_state_dir: Path to previous run's current-state
        current_column_dir: Path to current run's transformed column directory
        updated_table_qns: Set of table QNs with UPDATED state
        delete_column_dir: Path to write deleted column records
        chunk_idx: Chunk index for output file naming
        conn: Optional DuckDB connection to reuse.

    Returns:
        Number of deleted column records written
    """
    if not updated_table_qns:
        return 0

    prev_col_dir = previous_state_dir.joinpath(EntityType.COLUMN.value)
    if not prev_col_dir.exists():
        return 0

    prev_col_files = list(prev_col_dir.glob("*.json"))
    if not prev_col_files:
        return 0

    # Current columns may not exist (e.g., incremental run with no column extraction)
    current_col_files: List[Path] = []
    if current_column_dir.exists():
        current_col_files = list(current_column_dir.glob("*.json"))

    if not current_col_files:
        return 0

    with managed_duckdb_connection(conn) as active_conn:
        del_upd_prev_cols = "del_upd_prev_cols"
        del_upd_curr_cols = "del_upd_curr_cols"
        del_upd_tables = "del_upd_tables"

        active_conn.execute(f"DROP TABLE IF EXISTS {del_upd_prev_cols}")
        active_conn.execute(f"DROP TABLE IF EXISTS {del_upd_curr_cols}")
        active_conn.execute(f"DROP TABLE IF EXISTS {del_upd_tables}")

        table_qn_expr = get_parent_table_qn_expr()

        # Load previous columns with parent table QN
        active_conn.execute(f"""
            CREATE TABLE {del_upd_prev_cols} AS
            SELECT *, {table_qn_expr} AS parent_table_qn
            FROM {json_scan(prev_col_files)}
            WHERE {table_qn_expr} IS NOT NULL
        """)

        # Load current columns QNs for comparison
        active_conn.execute(f"""
            CREATE TABLE {del_upd_curr_cols} AS
            SELECT attributes.qualifiedName AS qualified_name
            FROM {json_scan(current_col_files)}
            WHERE attributes.qualifiedName IS NOT NULL
        """)

        # Lookup of updated table QNs
        active_conn.execute(f"CREATE TABLE {del_upd_tables} (table_qn VARCHAR)")
        active_conn.executemany(
            f"INSERT INTO {del_upd_tables} VALUES (?)",
            [(qn,) for qn in updated_table_qns],
        )

        # Find columns from previous state for UPDATED tables that are
        # missing in current extraction
        count_result = active_conn.execute(f"""
            SELECT COUNT(*)
            FROM {del_upd_prev_cols} pc
            JOIN {del_upd_tables} ut ON pc.parent_table_qn = ut.table_qn
            LEFT JOIN {del_upd_curr_cols} cc
              ON pc.attributes.qualifiedName = cc.qualified_name
            WHERE cc.qualified_name IS NULL
        """).fetchone()

        count = count_result[0] if count_result else 0
        if count == 0:
            return 0

        delete_column_dir.mkdir(parents=True, exist_ok=True)
        dest_file = delete_column_dir.joinpath(f"chunk-{chunk_idx}.json")
        dest_file_str = str(dest_file).replace("'", "''")

        active_conn.execute(f"""
            COPY (
                SELECT pc.* EXCLUDE (parent_table_qn)
                FROM {del_upd_prev_cols} pc
                JOIN {del_upd_tables} ut ON pc.parent_table_qn = ut.table_qn
                LEFT JOIN {del_upd_curr_cols} cc
                  ON pc.attributes.qualifiedName = cc.qualified_name
                WHERE cc.qualified_name IS NULL
            )
            TO '{dest_file_str}'
            (FORMAT JSON, ARRAY false)
        """)

        logger.info(
            "Detected %d deleted columns from %d updated tables",
            count,
            len(updated_table_qns),
        )
        return count


def _write_metadata(
    incremental_diff_dir: Path,
    result: IncrementalDiffResult,
) -> None:
    """Write metadata.json with entity counts for Argo routing.

    The metadata file is used by Argo templates to decide:
    - Stream publish: incremental-diff exists + has entities
    - Batch publish: no incremental-diff (full extraction)
    - Skip publish: incremental-diff exists + zero entities

    Args:
        incremental_diff_dir: Path to incremental-diff directory
        result: IncrementalDiffResult with entity counts
    """
    metadata: Dict[str, Any] = {
        "is_incremental": result.is_incremental,
        "tables_created": result.tables_created,
        "tables_updated": result.tables_updated,
        "tables_backfill": result.tables_backfill,
        "tables_deleted": result.tables_deleted,
        "columns_total": result.columns_total,
        "columns_deleted": result.columns_deleted,
        "schemas_total": result.schemas_total,
        "databases_total": result.databases_total,
        "total_changed_entities": result.total_changed_entities,
        "total_files": result.total_files,
    }

    metadata_path = incremental_diff_dir.joinpath("metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    logger.info("Wrote metadata.json: %s", metadata_path)


# =============================================================================
# Existing helper functions (unchanged)
# =============================================================================


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
