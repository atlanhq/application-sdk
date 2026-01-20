"""Helper utilities for incremental extraction.

This module provides:
- S3 path management for persistent artifacts
- Marker timestamp handling (download, normalize, prepone)
- Query batching utilities
- File utilities (copy, count)
"""

from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Sequence, Set, Tuple

import daft
import duckdb
from daft import DataFrame
from daft.functions import format as daft_format

from application_sdk.constants import (
    APPLICATION_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
    TEMPORARY_PATH,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore
from application_sdk.common.utils import generate_random_uuid

from application_sdk.common.incremental.constants import (
    MARKER_TIMESTAMP_FORMAT,
    PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE,
)
from application_sdk.common.incremental.models import EntityType, WorkflowMetadata
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
    DuckDBConnection,
    json_scan,
    get_parent_table_qn_expr,
    managed_duckdb_connection,
    escape_sql_string,
)

logger = get_logger(__name__)


# =============================================================================
# Connection ID Extraction
# =============================================================================


def extract_epoch_id_from_qualified_name(connection_qualified_name: str) -> str:
    """Extract the connection ID (epoch) from a connection qualified name.

    The connection qualified name follows the format: {tenant}/{connector}/{epoch}
    For example: "default/oracle/1764230875" -> "1764230875"

    Args:
        connection_qualified_name: The full qualified name

    Returns:
        The connection ID (epoch number) as a string

    Raises:
        ValueError: If the qualified name doesn't have the expected format
    """
    if not connection_qualified_name:
        raise ValueError("connection_qualified_name cannot be empty")

    parts = connection_qualified_name.split("/")

    if len(parts) < 3:
        raise ValueError(
            f"Could not extract epoch ID from connection_qualified_name: "
            f"'{connection_qualified_name}'. Expected format: 'tenant/connector/epoch'"
        )

    connection_id = parts[-1]

    if not connection_id.isdigit():
        logger.warning(
            f"Connection ID '{connection_id}' from '{connection_qualified_name}' "
            f"is not purely numeric. Using it anyway."
        )

    return connection_id


# =============================================================================
# Persistent Artifacts Helpers (S3 path management)
# =============================================================================


def get_persistent_s3_prefix(workflow_args: Dict[str, Any]) -> str:
    """Get the S3 key prefix for connection-scoped persistent artifacts.

    Returns:
        S3 key prefix like 'persistent-artifacts/apps/oracle/connection/1764230875'
    """
    if not workflow_args.get("connection", {}).get("connection_qualified_name"):
        raise ValueError("connection_qualified_name is required")

    connection_id = extract_epoch_id_from_qualified_name(
        workflow_args["connection"]["connection_qualified_name"]
    )

    application_name = workflow_args.get("application_name") or os.getenv(
        "ATLAN_APPLICATION_NAME", APPLICATION_NAME
    )

    s3_prefix = PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE.format(
        application_name=application_name,
        connection_id=connection_id,
    )

    logger.debug(f"S3 prefix for connection '{connection_id}' -> '{s3_prefix}'")
    return s3_prefix


def get_persistent_artifacts_path(
    workflow_args: Dict[str, Any], artifact_subpath: str
) -> Path:
    """Get local filesystem path for connection-scoped persistent artifacts.

    Args:
        workflow_args: Dictionary containing workflow configuration.
        artifact_subpath: Relative path under connection prefix.

    Returns:
        Local filesystem Path for the artifact.
    """
    s3_prefix = get_persistent_s3_prefix(workflow_args)
    return Path(TEMPORARY_PATH).joinpath(s3_prefix, artifact_subpath)


# =============================================================================
# Marker Timestamp Handling
# =============================================================================


def normalize_marker_timestamp(marker: str) -> str:
    """Remove nanoseconds from marker timestamp (e.g., .123456789Z -> Z)."""
    normalized = re.sub(r"\.\d{1,9}(?=Z$)", "", marker)
    if normalized != marker:
        logger.info(f"Normalized marker: '{marker}' → '{normalized}'")
    return normalized


def prepone_marker_timestamp(marker: str, hours: int) -> str:
    """Move marker timestamp back by specified hours.

    This handles edge cases where objects created very close to the marker
    timestamp might be missed.

    Args:
        marker: ISO 8601 timestamp string (e.g., '2025-01-15T10:30:00Z')
        hours: Number of hours to move the marker back

    Returns:
        Adjusted timestamp string in the same format
    """
    dt = datetime.strptime(marker, MARKER_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    adjusted = dt - timedelta(hours=hours)
    adjusted_str = adjusted.strftime(MARKER_TIMESTAMP_FORMAT)
    logger.info(f"Preponed marker by {hours}h: '{marker}' → '{adjusted_str}'")
    return adjusted_str


async def download_marker_from_s3(workflow_args: Dict[str, Any]) -> str | None:
    """Download marker.txt from S3 and return its content, or None if not found."""
    s3_prefix = get_persistent_s3_prefix(workflow_args)
    marker_s3_key = f"{s3_prefix}/marker.txt"
    local_marker_path = get_persistent_artifacts_path(workflow_args, "marker.txt")
    local_marker_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading marker from S3: {marker_s3_key}")
    try:
        await ObjectStore.download_file(
            source=marker_s3_key,
            destination=str(local_marker_path),
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )
        if local_marker_path.exists() and local_marker_path.stat().st_size > 0:
            marker = local_marker_path.read_text(encoding="utf-8").strip()
            logger.info(f"Marker downloaded: {marker}")
            return marker
        logger.info("Marker file downloaded but empty")
    except Exception as e:
        logger.info(f"Marker not found in S3 (first incremental run): {e}")
    return None


async def download_s3_prefix_with_structure(
    s3_prefix: str,
    local_destination: Path,
    store_name: str = UPSTREAM_OBJECT_STORE_NAME,
) -> None:
    """Download files from S3 preserving relative directory structure."""
    file_list = await ObjectStore.list_files(
        prefix=s3_prefix,
        store_name=store_name,
    )

    source_prefix = s3_prefix.rstrip("/")

    for file_path in file_list:
        if file_path.startswith(source_prefix):
            relative_path = file_path[len(source_prefix) :].lstrip("/")
        else:
            relative_path = file_path

        local_file_path = local_destination.joinpath(relative_path)
        local_file_path.parent.mkdir(parents=True, exist_ok=True)

        await ObjectStore.download_file(
            source=file_path,
            destination=str(local_file_path),
            store_name=store_name,
        )


# =============================================================================
# File Utilities
# =============================================================================


def count_json_files_recursive(directory: Path) -> int:
    """Count all JSON files recursively in a directory."""
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*.json"))


def copy_directory_parallel(
    source_dir: Path,
    dest_dir: Path,
    max_workers: int = 3,
) -> int:
    """Copy all files from source to destination directory in parallel.

    Args:
        source_dir: Source directory path
        dest_dir: Destination directory path
        max_workers: Maximum parallel copy workers

    Returns:
        Number of files copied
    """
    if not source_dir.exists():
        return 0

    files = list(source_dir.glob("*"))
    if not files:
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)

    def copy_file(src: Path) -> bool:
        try:
            shutil.copy2(src, dest_dir / src.name)
            return True
        except Exception as e:
            logger.warning(f"Failed to copy {src}: {e}")
            return False

    copied = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(copy_file, f): f for f in files if f.is_file()}
        for future in as_completed(futures):
            if future.result():
                copied += 1

    return copied


# =============================================================================
# Transformed Directory Helpers
# =============================================================================


def get_transformed_dir(workflow_args: Dict[str, Any]) -> Path:
    """Return current run's transformed directory.

    Caller must ensure files are downloaded from S3 before calling this method.
    """
    output_path_str = str(workflow_args.get("output_path", "")).strip()

    if not output_path_str:
        raise FileNotFoundError(
            "No output_path (parent path of transformed directory) provided"
        )

    transformed_dir = Path(output_path_str).joinpath("transformed")

    if not transformed_dir.exists() or not any(transformed_dir.rglob("*.json")):
        raise FileNotFoundError(
            f"Transformed directory not found or empty: {transformed_dir}. "
            f"Ensure files are downloaded from S3 first."
        )

    logger.info(f"Found transformed directory: {transformed_dir}")
    return transformed_dir


# =============================================================================
# Table Analysis for Column Extraction
# =============================================================================


def get_tables_needing_column_extraction(
    transformed_dir: Path,
    backfill_qualified_names: Set[str] | None = None,
) -> Tuple[DataFrame, int, int, int]:
    """Get Daft DataFrame of tables needing column extraction.

    Args:
        transformed_dir: Path to transformed directory with table JSON files
        backfill_qualified_names: Optional set of qualified names needing backfill

    Returns:
        Tuple of (filtered_df, changed_count, backfill_count, no_change_count)
    """
    try:
        backfill_qns = backfill_qualified_names or set()

        table_dir = transformed_dir.joinpath(EntityType.TABLE.value)
        if not table_dir.exists():
            raise FileNotFoundError(f"Transformed table directory not found: {table_dir}")

        json_files = [str(f) for f in table_dir.glob("*.json")]
        if not json_files:
            raise FileNotFoundError(f"No JSON files found in: {table_dir}")

        df = daft.read_json(json_files)

        df = df.select(
            daft.col("typeName"),
            daft.col("attributes").struct.get("databaseName").alias("database_name"),
            daft.col("attributes").struct.get("schemaName").alias("schema_name"),
            daft.col("attributes").struct.get("name").alias("table_name"),
            daft.col("attributes").struct.get("qualifiedName").alias("qualified_name"),
            daft.col("customAttributes")
            .struct.get("incremental_state")
            .alias("incremental_state"),
        )

        df = df.with_column(
            "table_id",
            daft_format("{}.{}.{}", "database_name", "schema_name", "table_name"),
        )

        # Get state counts
        state_counts_df = df.groupby("incremental_state").agg(
            daft.col("table_id").count().alias("cnt")
        )

        state_map: Dict[str, int] = {}
        for row in state_counts_df.iter_rows():
            state_map[row["incremental_state"]] = row["cnt"]

        created_count = state_map.get("CREATED", 0)
        updated_count = state_map.get("UPDATED", 0)
        no_change_count = state_map.get("NO CHANGE", 0)
        total_count = sum(state_map.values())

        logger.info(
            f"Analyzed {total_count} table records. "
            f"States: CREATED={created_count}, UPDATED={updated_count}, NO CHANGE={no_change_count}"
        )

        # Mark changed/backfill
        changed_states = ["CREATED", "UPDATED"]
        df = df.with_column(
            "is_changed",
            daft.col("incremental_state").is_in(changed_states),
        )

        if backfill_qns:
            df = df.with_column(
                "is_backfill",
                (~daft.col("is_changed"))
                & daft.col("qualified_name").is_in(list(backfill_qns)),
            )
        else:
            df = df.with_column("is_backfill", daft.lit(False))

        filtered_df = df.where(
            daft.col("is_changed") | daft.col("is_backfill")
        ).select("table_id", "qualified_name", "is_changed", "is_backfill")

        # Compute counts
        counts_df = filtered_df.agg(
            daft.col("is_changed").cast(daft.DataType.int64()).sum().alias("changed_count"),
            daft.col("is_backfill").cast(daft.DataType.int64()).sum().alias("backfill_count"),
        )
        counts_row = list(counts_df.iter_rows())[0]
        changed_count = int(counts_row["changed_count"] or 0)
        backfill_count = int(counts_row["backfill_count"] or 0)

        logger.info(
            f"Tables needing column extraction: {changed_count + backfill_count} "
            f"({changed_count} changed, {backfill_count} backfill)"
        )

        filtered_df = filtered_df.select("table_id", "is_changed", "is_backfill")
        return filtered_df, changed_count, backfill_count, no_change_count

    except Exception as e:
        logger.error(f"Daft table analysis failed: {e}")
        raise


def get_backfill_tables(
    current_transformed_dir: Path, previous_current_state_dir: Path | None
) -> Set[str] | None:
    """Use DuckDB to compare current tables vs previous current-state.

    Returns qualified names of tables that need backfilling.
    """
    if not previous_current_state_dir or not previous_current_state_dir.exists():
        logger.info("No previous state available - returning None")
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
                raise ValueError("No transformed tables found")

            if previous_tables is None or previous_tables == 0:
                logger.warning("Previous state exists but contains no table files")
                return None

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
                logger.info(f"Found {len(backfill_qns)} assets needing backfill")
            else:
                logger.info("No backfill tables found")

            return backfill_qns

    except Exception as e:
        logger.error(f"DuckDB analysis failed: {e}, returning None")
        return None


def _load_tables_to_duckdb(
    conn: duckdb.DuckDBPyConnection, base_dir: Path, table_name: str
) -> int | None:
    """Load table JSON files into DuckDB."""
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

    union_parts = []
    for json_file in json_files:
        escaped_file = str(json_file.resolve()).replace("'", "''")
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
        logger.error(f"DuckDB failed to load JSON files: {e}")
        raise

    result = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return result[0] if result else None


# =============================================================================
# Query Generation Helpers
# =============================================================================


def render_column_sql(
    template_sql: str, table_ids: Sequence[str], schema_name: str, marker: str
) -> str:
    """Render column extraction SQL with table filter CTE.

    This is a generic implementation. Override for database-specific syntax.
    """
    if not table_ids:
        raise ValueError("No table IDs provided for column query generation")

    # Build VALUES list for table_filter CTE (generic SQL syntax)
    values_list = ", ".join(f"('{tid.replace(chr(39), chr(39)+chr(39))}')" for tid in table_ids)
    table_filter_cte = f"WITH table_filter AS (SELECT * FROM (VALUES {values_list}) AS t(TABLE_ID))"

    rendered = template_sql.replace("--TABLE_FILTER_CTE--", table_filter_cte)
    rendered = rendered.replace(":schema_name", schema_name)
    rendered = rendered.replace(":marker_timestamp", marker)
    rendered = rendered.replace("{system_schema}", schema_name)
    rendered = rendered.replace("{marker_timestamp}", marker)
    return rendered


def ensure_runtime_queries_dir(workflow_args: Dict[str, Any], subdir: str) -> Path:
    """Ensure queries output directory exists and return its path."""
    output_path_str = str(workflow_args.get("output_path", "")).strip()

    if not output_path_str:
        raise ValueError("Cannot determine queries directory - no output_path provided")

    qdir = Path(output_path_str).joinpath("queries", subdir)
    qdir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Created queries directory: {qdir}")
    return qdir


def write_batched_queries_from_df(
    template_sql: str,
    df: DataFrame,
    batch_size: int,
    output_dir: Path,
    schema_name: str,
    marker: str,
    render_fn=None,
) -> Generator[Path, None, None]:
    """Yield SQL query file paths, streaming table_ids from Daft DataFrame.

    Args:
        template_sql: SQL template with --TABLE_FILTER_CTE-- placeholder
        df: Daft DataFrame with 'table_id' column
        batch_size: Maximum number of tables per batch
        output_dir: Directory to write query files
        schema_name: Schema name for placeholders
        marker: Marker timestamp for placeholders
        render_fn: Optional custom render function (defaults to render_column_sql)

    Yields:
        Path to each generated SQL file
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    render = render_fn or render_column_sql

    batch_idx = 0
    total_tables = 0
    current_batch: List[str] = []

    for row in df.select("table_id").iter_rows():
        current_batch.append(row["table_id"])

        if len(current_batch) >= batch_size:
            sql = render(template_sql, current_batch, schema_name, marker)
            batch_file = output_dir.joinpath(f"column_query-{batch_idx}.sql")
            batch_file.write_text(sql, encoding="utf-8")

            batch_idx += 1
            total_tables += len(current_batch)
            yield batch_file

            current_batch = []

    # Write remaining
    if current_batch:
        sql = render(template_sql, current_batch, schema_name, marker)
        batch_file = output_dir / f"column_query-{batch_idx}.sql"
        batch_file.write_text(sql, encoding="utf-8")

        batch_idx += 1
        total_tables += len(current_batch)
        yield batch_file

    logger.info(
        f"Generated {batch_idx} query files for {total_tables} tables "
        f"in directory: {output_dir}"
    )
