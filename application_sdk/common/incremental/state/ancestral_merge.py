"""Ancestral column merging utilities.

This module handles merging current extraction results with ancestral state
to preserve column metadata for NO CHANGE tables across incremental runs.

Key concepts:
- Current columns (CREATED/UPDATED tables) → Use from current extraction
- Ancestral columns (NO CHANGE tables) → Carry forward from previous state
- Deleted/filtered-out tables → Exclude from new state

First Run Semantics:
- All tables are treated as INCREMENTAL_DEFAULT_STATE ('NO CHANGE') for merge logic
- This means: use columns from current extraction (no ancestral state exists)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Set, Tuple

from application_sdk.common.incremental.models import (
    ColumnField,
    MergeResult,
    TableScope,
)
from application_sdk.common.incremental.state.table_scope import (
    get_table_qns_from_columns,
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


def merge_ancestral_columns(
    current_transformed_dir: Path,
    previous_state_dir: Path | None,
    new_state_dir: Path,
    table_scope: TableScope,
    column_chunk_size: int,
    conn: DuckDBConnection = None,
) -> Tuple[MergeResult, Set[str]]:
    """Merge current columns with ancestral columns for NO CHANGE tables.

    This method implements ancestral state preservation by consolidating
    all columns (current + ancestral) into properly sized chunk files.

    Key behavior:
    - Current columns (CREATED/UPDATED tables) → From current extraction
    - Ancestral columns (NO CHANGE tables) → From previous state
    - All columns are consolidated into files based on column_chunk_size
    - Only creates multiple files if total exceeds chunk_size

    Args:
        current_transformed_dir: Path to current run's transformed output
        previous_state_dir: Path to previous run's current-state (may be None)
        new_state_dir: Path to write merged current state
        table_scope: Current table scope with incremental states
        column_chunk_size: Maximum records per chunk file (default: 100,000)
        conn: Optional DuckDB connection to reuse. If None, creates a new connection.

    Returns:
        Tuple of (MergeResult with counts, Set of tables with extracted columns)
    """
    tables_with_extracted_columns: Set[str] = set()

    new_column_dir = new_state_dir.joinpath("column")
    new_column_dir.mkdir(parents=True, exist_ok=True)

    current_column_dir = current_transformed_dir.joinpath("column")

    # Collect file paths for DuckDB processing
    current_json_files: List[Path] = (
        list(current_column_dir.glob("*.json")) if current_column_dir.exists() else []
    )
    ancestral_json_files: List[Path] = []

    if previous_state_dir and previous_state_dir.exists():
        ancestral_column_dir = previous_state_dir.joinpath("column")
        if ancestral_column_dir.exists():
            ancestral_json_files = list(ancestral_column_dir.glob("*.json"))

    # Get tables that have columns from current extraction
    if current_json_files:
        logger.info(
            f"Processing {len(current_json_files)} column files from {current_column_dir}"
        )
        tables_with_columns = get_table_qns_from_columns(current_column_dir, conn=conn)
        if tables_with_columns is None:
            raise RuntimeError(
                f"Failed to determine tables with columns from {current_column_dir}. "
                f"Found {len(current_json_files)} JSON files but couldn't parse table references. "
                f"Check logs for '{get_table_qns_from_columns.__name__}' for details. "
                f"This would cause duplicate columns in the merged state."
            )
        tables_with_extracted_columns = tables_with_columns
        logger.info(
            f"Found {len(tables_with_extracted_columns)} tables with extracted columns"
        )

    # Tables needing ancestral = in current run's scope but not already extracted
    all_table_qns = set(iter_scope_table_qns(table_scope))
    tables_needing_ancestral = all_table_qns - tables_with_extracted_columns

    with managed_duckdb_connection(conn) as active_conn:
        result = _consolidate_columns(
            conn=active_conn,
            current_json_files=current_json_files,
            ancestral_json_files=ancestral_json_files,
            new_column_dir=new_column_dir,
            tables_needing_ancestral=tables_needing_ancestral,
            tables_with_extracted_columns=tables_with_extracted_columns,
            column_chunk_size=column_chunk_size,
        )

    logger.info(
        f"Column merge complete: {result.columns_total} total "
        f"(current={result.columns_from_current}, ancestral={result.columns_from_ancestral}), "
        f"excluded={result.excluded_already_extracted + result.excluded_table_removed} "
        f"(already_extracted={result.excluded_already_extracted}, "
        f"table_removed={result.excluded_table_removed})"
    )

    return result, tables_with_extracted_columns


def _consolidate_columns(
    conn: Any,
    current_json_files: List[Path],
    ancestral_json_files: List[Path],
    new_column_dir: Path,
    tables_needing_ancestral: Set[str],
    tables_with_extracted_columns: Set[str],
    column_chunk_size: int,
) -> MergeResult:
    """Consolidate current and ancestral column metadata into chunked JSONL files.

    Semantics:
    - Current columns are always taken as-is from the current extraction
    - Ancestral columns are only included for NO CHANGE tables
    - Ancestral columns always receive incremental_state = NO CHANGE
    - Deleted or re-extracted tables never contribute ancestral columns

    Output:
    - chunk-{i}-part0.json files written to new_column_dir
    - One JSON object per line (newline-delimited JSON / JSONL format)
    """
    result = MergeResult()

    # ------------------------------------------------------------------
    # Canonical column schema (do NOT infer dynamically)
    # ------------------------------------------------------------------
    COLUMN_FIELDS = [field.value for field in ColumnField]
    column_list = ", ".join(COLUMN_FIELDS)
    ancestral_column_list = ", ".join(
        f"a.{field.value}"
        for field in ColumnField
        if field != ColumnField.CUSTOM_ATTRIBUTES
    )

    # ------------------------------------------------------------------
    # Unique DuckDB object names to avoid conflicts when connection is reused
    # ------------------------------------------------------------------
    ancestral_tables_lookup = "merge_ancestral_tables_lookup"
    current_columns_data = "merge_current_columns_data"
    all_ancestral_columns_data = "merge_all_ancestral_columns_data"
    filtered_ancestral_columns_view = "merge_filtered_ancestral_columns_view"
    combined_columns_view = "merge_combined_columns_view"

    # Clean up any existing tables/views from previous runs
    for name in [
        ancestral_tables_lookup,
        current_columns_data,
        all_ancestral_columns_data,
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    for name in [filtered_ancestral_columns_view, combined_columns_view]:
        conn.execute(f"DROP VIEW IF EXISTS {name}")

    # ------------------------------------------------------------------
    # Lookup table: tables that are allowed to reuse ancestral columns
    # ------------------------------------------------------------------
    conn.execute(f"CREATE TABLE {ancestral_tables_lookup} (table_qn VARCHAR)")
    if tables_needing_ancestral:
        conn.executemany(
            f"INSERT INTO {ancestral_tables_lookup} VALUES (?)",
            [(qn,) for qn in tables_needing_ancestral],
        )

    # ------------------------------------------------------------------
    # Step 1: Load current columns
    # ------------------------------------------------------------------
    if current_json_files:
        current_scan = json_scan(current_json_files)
        conn.execute(f"""
            CREATE TABLE {current_columns_data} AS
            SELECT {column_list}
            FROM {current_scan}
        """)
        count_result = conn.execute(
            f"SELECT COUNT(*) FROM {current_columns_data}"
        ).fetchone()
        result.columns_from_current = count_result[0] if count_result else 0

        logger.info(
            f"Loaded {result.columns_from_current} current columns "
            f"from {len(current_json_files)} files"
        )
    else:
        conn.execute(
            f"CREATE TABLE {current_columns_data} AS SELECT * FROM (SELECT 1) WHERE FALSE"
        )
        logger.info("No current columns found")

    # ------------------------------------------------------------------
    # Step 2: Load and filter ancestral columns
    # ------------------------------------------------------------------
    if ancestral_json_files and tables_needing_ancestral:
        ancestral_scan = json_scan(ancestral_json_files)
        parent_table_qn_expr = get_parent_table_qn_expr()

        # Load ancestral rows with derived parent table qualified name
        conn.execute(f"""
            CREATE TABLE {all_ancestral_columns_data} AS
            SELECT
                *,
                {parent_table_qn_expr} AS parent_table_qn
            FROM {ancestral_scan}
            WHERE {parent_table_qn_expr} IS NOT NULL
        """)

        # Count valid ancestral columns
        count_result = conn.execute(f"""
            SELECT COUNT(*)
            FROM {all_ancestral_columns_data} a
            JOIN {ancestral_tables_lookup} v
              ON a.parent_table_qn = v.table_qn
        """).fetchone()
        result.columns_from_ancestral = count_result[0] if count_result else 0

        # Count excluded columns (for metrics only)
        excluded_rows = conn.execute(f"""
            SELECT a.parent_table_qn, COUNT(*) AS cnt
            FROM {all_ancestral_columns_data} a
            LEFT JOIN {ancestral_tables_lookup} v
              ON a.parent_table_qn = v.table_qn
            WHERE v.table_qn IS NULL
            GROUP BY a.parent_table_qn
        """).fetchall()

        for table_qn, cnt in excluded_rows:
            if table_qn in tables_with_extracted_columns:
                result.excluded_already_extracted += cnt
            else:
                result.excluded_table_removed += cnt

        # Create final ancestral view with enforced incremental_state
        conn.execute(f"""
            CREATE VIEW {filtered_ancestral_columns_view} AS
            SELECT
                {ancestral_column_list},
                json_merge_patch(
                    COALESCE(a.customAttributes, '{{}}'::JSON),
                    '{{"incremental_state":"NO CHANGE"}}'::JSON
                ) AS customAttributes
            FROM {all_ancestral_columns_data} a
            JOIN {ancestral_tables_lookup} v
            ON a.parent_table_qn = v.table_qn
        """)

        logger.info(
            f"Loaded {result.columns_from_ancestral} ancestral columns "
            f"from {len(ancestral_json_files)} files"
        )
    else:
        conn.execute(
            f"CREATE VIEW {filtered_ancestral_columns_view} AS "
            f"SELECT * FROM (SELECT 1) WHERE FALSE"
        )
        if not ancestral_json_files:
            logger.info("No ancestral column files available")
        else:
            logger.info("No tables eligible for ancestral column reuse")

    # ------------------------------------------------------------------
    # Step 3: Combine current + ancestral
    # ------------------------------------------------------------------
    result.columns_total = result.columns_from_current + result.columns_from_ancestral

    if result.columns_total == 0:
        logger.info("No columns to write")
        return result

    if result.columns_from_current and result.columns_from_ancestral:
        conn.execute(f"""
            CREATE VIEW {combined_columns_view} AS
            SELECT {column_list} FROM {current_columns_data}
            UNION ALL
            SELECT {column_list} FROM {filtered_ancestral_columns_view}
        """)
    elif result.columns_from_current:
        conn.execute(
            f"CREATE VIEW {combined_columns_view} AS "
            f"SELECT {column_list} FROM {current_columns_data}"
        )
    else:
        conn.execute(
            f"CREATE VIEW {combined_columns_view} AS "
            f"SELECT {column_list} FROM {filtered_ancestral_columns_view}"
        )

    # ------------------------------------------------------------------
    # Step 4: Write chunked JSONL output using DuckDB COPY TO
    # ------------------------------------------------------------------
    total = result.columns_total
    num_chunks = (total + column_chunk_size - 1) // column_chunk_size

    files_written = 0

    for chunk_idx in range(num_chunks):
        offset = chunk_idx * column_chunk_size
        dest_file = new_column_dir / f"chunk-{chunk_idx}-part0.json"

        dest_path = str(dest_file).replace("'", "''")
        conn.execute(
            f"""
            COPY (
                SELECT {column_list}
                FROM {combined_columns_view}
                LIMIT {column_chunk_size}
                OFFSET {offset}
            )
            TO '{dest_path}'
            (FORMAT JSON, ARRAY false)
        """
        )

        files_written += 1

    logger.info(
        f"Wrote {result.columns_total} columns across {files_written} chunk files "
        f"(chunk_size={column_chunk_size})"
    )

    return result
