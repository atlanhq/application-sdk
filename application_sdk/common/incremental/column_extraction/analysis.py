"""Daft-based table state analysis for incremental column extraction.

This module uses Daft's lazy evaluation to efficiently analyze transformed
table JSON files and identify which tables need column extraction based on
their incremental state (CREATED, UPDATED, or BACKFILL).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Set, Tuple

import daft
from daft import DataFrame
from daft.functions import format as daft_format

from application_sdk.common.incremental.models import EntityType
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def get_transformed_dir(workflow_args: Dict[str, Any]) -> Path:
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

    logger.info(f"Found transformed directory: {transformed_dir}")
    return transformed_dir


def get_tables_needing_column_extraction(
    transformed_dir: Path,
    backfill_qualified_names: Set[str] | None = None,
) -> Tuple[DataFrame, int, int, int]:
    """Get Daft DataFrame of tables needing column extraction.

    All filtering and categorization is done within Daft's lazy evaluation.
    Returns a DataFrame that can be iterated efficiently.

    Args:
        transformed_dir: Path to transformed directory with table JSON files.
        backfill_qualified_names: Optional set of qualified names needing backfill.

    Returns:
        Tuple of:
        - filtered_df: Daft DataFrame with table_id, is_changed, is_backfill columns
        - changed_count: Number of tables created or updated
        - backfill_count: Number of backfill tables
        - no_change_count: Number of unchanged tables
    """
    try:
        backfill_qns = backfill_qualified_names or set()

        table_dir = transformed_dir.joinpath(EntityType.TABLE.value)
        if not table_dir.exists():
            raise FileNotFoundError(
                f"Transformed table directory not found: {table_dir}"
            )

        json_files = [str(f) for f in table_dir.glob("*.json")]
        if not json_files:
            raise FileNotFoundError(f"No JSON files found in: {table_dir}")

        # read_json does lazy loading
        df = daft.read_json(json_files)

        df = df.select(
            daft.col("typeName"),
            daft.col("attributes").get("databaseName").alias("database_name"),
            daft.col("attributes").get("schemaName").alias("schema_name"),
            daft.col("attributes").get("name").alias("table_name"),
            daft.col("attributes").get("qualifiedName").alias("qualified_name"),
            daft.col("customAttributes")
            .get("incremental_state")
            .alias("incremental_state"),
        )

        # Build table_id column: catalog.schema.table
        df = df.with_column(
            "table_id",
            daft_format("{}.{}.{}", "database_name", "schema_name", "table_name"),
        )

        # Get state counts using Daft aggregation (tiny result - max 3 rows)
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
            f"Analyzed {total_count} table records from {len(json_files)} files. "
            f"States: CREATED={created_count}, UPDATED={updated_count}, "
            f"NO CHANGE={no_change_count}"
        )

        # Mark changed/backfill rows in Daft
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

        # Filter to only tables needing extraction
        filtered_df = df.where(daft.col("is_changed") | daft.col("is_backfill")).select(
            "table_id", "qualified_name", "is_changed", "is_backfill"
        )

        # Compute extraction counts using Daft aggregation
        counts_df = filtered_df.agg(
            daft.col("is_changed")
            .cast(daft.DataType.int64())
            .sum()
            .alias("changed_count"),
            daft.col("is_backfill")
            .cast(daft.DataType.int64())
            .sum()
            .alias("backfill_count"),
        )
        counts_row = list(counts_df.iter_rows())[0]
        changed_count = int(counts_row["changed_count"] or 0)
        backfill_count = int(counts_row["backfill_count"] or 0)

        logger.info(
            f"Tables needing column extraction: {changed_count + backfill_count} "
            f"({changed_count} changed, {backfill_count} backfill)"
        )

        # Return only the columns needed for query generation
        filtered_df = filtered_df.select("table_id", "is_changed", "is_backfill")

        return filtered_df, changed_count, backfill_count, no_change_count

    except Exception as e:
        logger.error(f"Daft table analysis failed: {e}")
        raise
