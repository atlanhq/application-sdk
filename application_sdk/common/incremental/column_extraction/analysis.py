"""DuckDB-based table state analysis for incremental column extraction.

This module uses DuckDB to efficiently analyze transformed table JSON files
and identify which tables need column extraction based on their incremental
state (CREATED, UPDATED, or BACKFILL).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from application_sdk.common.incremental.models import EntityType
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def get_transformed_dir(workflow_args: dict[str, Any]) -> Path:
    """Return current run's transformed directory.

    Caller must ensure files are downloaded from S3 before calling this.
    Files are at output_path/transformed after ObjectStore.download_prefix().

    Args:
        workflow_args: Dictionary containing workflow configuration with output_path.

    Returns:
        Path to the transformed directory.

    Raises:
        FileNotFoundError: If output_path is missing or transformed directory is empty.
    """
    output_path_str = str(workflow_args.get("output_path", "")).strip()

    if not output_path_str:
        raise FileNotFoundError(
            "No output_path (parent path of transformed directory) "
            "provided in workflow_args"
        )

    transformed_dir = Path(output_path_str).joinpath("transformed")

    if not transformed_dir.exists() or not any(transformed_dir.rglob("*.json")):
        raise FileNotFoundError(
            f"Transformed directory not found or empty: {transformed_dir}. "
            f"Ensure files are downloaded from S3 first."
        )

    logger.info("Found transformed directory: %s", transformed_dir)
    return transformed_dir


def get_tables_needing_column_extraction(
    transformed_dir: Path,
    backfill_qualified_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Get tables needing column extraction using DuckDB.

    Reads transformed table JSON files and identifies which tables need
    column extraction based on their incremental state (CREATED, UPDATED, or BACKFILL).

    Args:
        transformed_dir: Path to transformed directory with table JSON files.
        backfill_qualified_names: Optional set of qualified names needing backfill.

    Returns:
        Tuple of:
        - rows: list of dicts with table_id, is_changed, is_backfill keys
        - changed_count: Number of tables created or updated
        - backfill_count: Number of backfill tables
        - no_change_count: Number of unchanged tables
    """
    try:
        from application_sdk.common.incremental.storage.duckdb_utils import (  # noqa: PLC0415 — optional dep: duckdb
            DuckDBConnectionManager,
            escape_sql_string,
            json_scan,
        )

        backfill_qns = backfill_qualified_names or set()

        table_dir = transformed_dir.joinpath(EntityType.TABLE.value)
        if not table_dir.exists():
            raise FileNotFoundError(
                f"Transformed table directory not found: {table_dir}"
            )

        json_files = [f for f in table_dir.glob("*.json")]
        if not json_files:
            raise FileNotFoundError(f"No JSON files found in: {table_dir}")

        json_source = json_scan(json_files)

        with DuckDBConnectionManager() as db:
            conn = db.connection

            # Base query: extract fields from the JSON source.
            # attributes may come through as a STRUCT when inferred by DuckDB;
            # to_json() normalises it to a JSON string before json_extract_string.
            base_sql = f"""
            SELECT
                json_extract_string(to_json(attributes), '$.databaseName') AS database_name,
                json_extract_string(to_json(attributes), '$.schemaName')   AS schema_name,
                json_extract_string(to_json(attributes), '$.name')         AS table_name,
                json_extract_string(to_json(attributes), '$.qualifiedName') AS qualified_name,
                COALESCE(
                    json_extract_string(to_json(customAttributes), '$.incremental_state'),
                    'NO CHANGE'
                ) AS incremental_state
            FROM {json_source}
            """

            # State counts — tiny result, max 3 distinct states
            state_counts = conn.execute(f"""
            SELECT incremental_state, COUNT(*) AS cnt
            FROM ({base_sql})
            GROUP BY incremental_state
            """).fetchall()

            state_map: dict[str, int] = {row[0]: row[1] for row in state_counts}
            created_count = state_map.get("CREATED", 0)
            updated_count = state_map.get("UPDATED", 0)
            no_change_count = state_map.get("NO CHANGE", 0)
            total_count = sum(state_map.values())

            logger.info(
                "Analyzed %d table records from %d files "
                "(created=%d updated=%d no_change=%d)",
                total_count,
                len(json_files),
                created_count,
                updated_count,
                no_change_count,
            )

            # Build backfill filter expression, passing values via SQL literals
            # (escape_sql_string guards against single-quote injection)
            if backfill_qns:
                backfill_values = ", ".join(
                    f"('{escape_sql_string(qn)}')" for qn in backfill_qns
                )
                backfill_filter = f"qualified_name IN (SELECT qn FROM (VALUES {backfill_values}) t(qn))"
            else:
                backfill_filter = "FALSE"

            result_rows = conn.execute(f"""
            SELECT
                database_name || '.' || schema_name || '.' || table_name AS table_id,
                incremental_state IN ('CREATED', 'UPDATED') AS is_changed,
                (incremental_state NOT IN ('CREATED', 'UPDATED'))
                    AND ({backfill_filter}) AS is_backfill
            FROM ({base_sql})
            WHERE incremental_state IN ('CREATED', 'UPDATED')
               OR ({backfill_filter})
            """).fetchall()

        rows = [
            {
                "table_id": row[0],
                "is_changed": bool(row[1]),
                "is_backfill": bool(row[2]),
            }
            for row in result_rows
        ]

        changed_count = sum(1 for r in rows if r["is_changed"])
        backfill_count = sum(1 for r in rows if r["is_backfill"])

        logger.info(
            "Tables needing column extraction: total=%d changed=%d backfill=%d",
            changed_count + backfill_count,
            changed_count,
            backfill_count,
        )

        return rows, changed_count, backfill_count, no_change_count

    # conformance: ignore[E004] re-raises immediately as typed ColumnExtractionAnalysisError; no swallow occurs
    except Exception as e:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            ColumnExtractionAnalysisError,
        )

        raise ColumnExtractionAnalysisError(cause=e) from e
