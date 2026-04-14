"""Current state writer utilities for incremental extraction.

This module provides helper functions for creating and uploading current state
snapshots during incremental metadata extraction workflows.

The state writer is responsible for:
1. Downloading transformed data from S3
2. Preparing current-state directory structure
3. Copying entity data (tables, schemas, databases)
4. Merging ancestral column data for unchanged tables
5. Creating incremental diffs for changed entities
6. Uploading the final snapshot to S3

Example workflow:
    1. download_transformed_data() - Get current run's transformed output
    2. prepare_previous_state() - Download previous state for comparison
    3. copy_entity_data() - Copy non-column entities to current state
    4. Merge columns using ancestral_merge module
    5. Create incremental diff
    6. upload_current_state() - Upload to S3

High-level orchestration:
    Use create_current_state_snapshot() for complete state creation including
    merge and diff generation in a single call.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Set

from application_sdk.common.exc_utils import rewrap
from application_sdk.common.incremental.helpers import (
    copy_directory_parallel,
    count_json_files_recursive,
    download_s3_prefix_with_structure,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
)
from application_sdk.common.incremental.models import EntityType
from application_sdk.common.incremental.state.ancestral_merge import (
    merge_ancestral_columns,
)
from application_sdk.common.incremental.state.incremental_diff import (
    create_incremental_diff,
)
from application_sdk.common.incremental.state.table_scope import (
    close_scope,
    get_current_table_scope,
    get_scope_length,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.constants import INCREMENTAL_DIFF_SUBPATH_TEMPLATE
from application_sdk.execution._temporal.activity_utils import get_object_store_prefix
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.ops import download_prefix, upload_prefix

logger = get_logger(__name__)


@dataclass
class CurrentStateResult:
    """Result of current state creation operation.

    Attributes:
        current_state_dir: Local path to current state directory
        current_state_s3_prefix: S3 prefix where state was uploaded
        total_files: Total JSON files in current state
        incremental_diff_dir: Local path to incremental diff (if created)
        incremental_diff_s3_prefix: S3 prefix for diff (if uploaded)
        incremental_diff_files: Number of files in diff (0 if not created)
    """

    current_state_dir: Path
    current_state_s3_prefix: str
    total_files: int
    incremental_diff_dir: Optional[Path] = None
    incremental_diff_s3_prefix: Optional[str] = None
    incremental_diff_files: int = 0


async def download_transformed_data(output_path: str) -> Path:
    """Download transformed files from S3 to local storage.

    Downloads the current workflow run's transformed output from S3,
    which contains the freshly extracted and transformed metadata.

    Args:
        output_path: Local output path from workflow_args (e.g., ./local/tmp/wf-123/run-456)

    Returns:
        Path to local transformed directory

    Raises:
        FileNotFoundError: If output_path is empty or invalid

    Example:
        >>> transformed_dir = await download_transformed_data("./local/tmp/wf-123/run-456")
        >>> print(f"Transformed data in: {transformed_dir}")
    """
    output_path_str = str(output_path).strip()
    if not output_path_str:
        raise FileNotFoundError("No output_path provided in workflow_args")

    transformed_local_path = os.path.join(output_path_str, "transformed")
    transformed_s3_prefix = get_object_store_prefix(transformed_local_path)

    logger.info(
        "Downloading transformed files from S3", s3_prefix=transformed_s3_prefix
    )

    # Ensure local directory exists before download
    transformed_dir = Path(transformed_local_path)
    transformed_dir.mkdir(parents=True, exist_ok=True)

    await download_prefix(
        prefix=transformed_s3_prefix,
        local_dir=str(transformed_dir),
    )

    return transformed_dir


async def prepare_previous_state(
    connection_qualified_name: str,
    current_state_available: bool,
    current_state_dir: Path,
    application_name: str = "",
) -> Optional[Path]:
    """Download previous state to a temporary location for comparison.

    When previous state exists, downloads it to a temporary directory
    to support ancestral column merging and incremental diff generation.

    Args:
        connection_qualified_name: The connection qualified name.
        current_state_available: Whether previous state exists in S3
        current_state_dir: Path to current-state directory
        application_name: Optional application name override.

    Returns:
        Path to temporary previous state directory, or None if no previous state

    Example:
        >>> prev_dir = await prepare_previous_state(
        ...     connection_qualified_name="default/oracle/1764230875",
        ...     current_state_available=True,
        ...     current_state_dir=current_state_dir,
        ... )
        >>> if prev_dir:
        ...     # Use previous state for comparison
        ...     pass
    """
    if not current_state_available:
        return None

    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    current_state_s3_prefix = f"{s3_prefix}/current-state"

    previous_state_temp_dir = current_state_dir.parent.joinpath(
        f"{current_state_dir.name}.previous"
    )

    # Clean up any existing temp directory from previous failed runs
    if previous_state_temp_dir.exists():
        shutil.rmtree(previous_state_temp_dir)
    previous_state_temp_dir.mkdir(parents=True, exist_ok=True)

    # Download previous state from S3 to temporary location
    logger.info("Downloading previous state from S3: %s", current_state_s3_prefix)
    try:
        await download_s3_prefix_with_structure(
            s3_prefix=current_state_s3_prefix,
            local_destination=previous_state_temp_dir,
        )
        logger.info(
            "Previous state downloaded to temporary location",
            path=str(previous_state_temp_dir),
        )
        return previous_state_temp_dir
    except Exception as e:
        if previous_state_temp_dir.exists():
            shutil.rmtree(previous_state_temp_dir)
        raise rewrap(e, "Failed to download previous state") from e


def copy_non_column_entities(
    transformed_dir: Path,
    current_state_dir: Path,
    copy_workers: int = 4,
) -> Dict[str, int]:
    """Copy non-column entity files from transformed to current-state.

    Copies table, schema, and database entity JSON files from the current
    run's transformed output to the current-state directory. These entities
    always use the current transformed data (not ancestral).

    Args:
        transformed_dir: Path to transformed output directory
        current_state_dir: Path to current-state directory
        copy_workers: Number of parallel workers for copy operations

    Returns:
        Dictionary mapping entity type to number of files copied

    Example:
        >>> counts = copy_non_column_entities(transformed_dir, state_dir)
        >>> print(f"Copied {counts['table']} table files")
    """
    copy_counts: Dict[str, int] = {}

    for entity_type in [EntityType.TABLE, EntityType.SCHEMA, EntityType.DATABASE]:
        entity_dir = transformed_dir.joinpath(entity_type.value)
        if entity_dir.exists():
            dest_dir = current_state_dir.joinpath(entity_type.value)
            count = copy_directory_parallel(
                entity_dir, dest_dir, max_workers=copy_workers
            )
            copy_counts[entity_type.value] = count
            logger.info(
                "Copied entity files to current state",
                entity_type=entity_type.value,
                count=count,
            )

    return copy_counts


async def upload_current_state(
    current_state_dir: Path,
    connection_qualified_name: str,
    application_name: str = "",
) -> str:
    """Upload current-state snapshot to S3.

    Uploads the finalized current-state directory to S3 for persistence
    across workflow runs. This becomes the "previous state" for the next run.

    Args:
        current_state_dir: Path to local current-state directory
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.

    Returns:
        S3 prefix where current-state was uploaded

    Example:
        >>> s3_prefix = await upload_current_state(
        ...     state_dir,
        ...     connection_qualified_name="default/oracle/1764230875",
        ... )
        >>> print(f"Uploaded to: {s3_prefix}")
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    current_state_s3_prefix = f"{s3_prefix}/current-state"

    await upload_prefix(
        local_dir=str(current_state_dir),
        prefix=current_state_s3_prefix,
    )
    logger.info("Current-state uploaded to S3: %s", current_state_s3_prefix)

    return current_state_s3_prefix


def cleanup_previous_state(previous_state_dir: Optional[Path]) -> None:
    """Clean up temporary previous state directory.

    Removes the temporary directory used for storing previous state
    during the merge operation.

    Args:
        previous_state_dir: Path to temporary previous state directory, or None

    Example:
        >>> cleanup_previous_state(prev_dir)
    """
    if previous_state_dir and previous_state_dir.exists():
        try:
            shutil.rmtree(previous_state_dir)
            logger.info(
                "Cleaned up temporary previous state directory",
                path=str(previous_state_dir),
            )
        except Exception:
            # Non-critical cleanup failure - log warning but don't raise
            logger.warning(
                "Failed to clean up temporary previous state directory", exc_info=True
            )


def prepare_current_state_directory(current_state_dir: Path) -> None:
    """Clear and recreate current-state directory.

    Ensures a clean slate for the current state by removing any existing
    directory and creating a fresh one.

    Args:
        current_state_dir: Path to current-state directory
    """
    if current_state_dir.exists():
        shutil.rmtree(current_state_dir)
    current_state_dir.mkdir(parents=True, exist_ok=True)


async def create_current_state_snapshot(
    connection_qualified_name: str,
    transformed_dir: Path,
    previous_state_dir: Optional[Path],
    current_state_dir: Path,
    s3_prefix: str,
    run_id: str,
    application_name: str = "",
    copy_workers: int = 4,
    column_chunk_size: int = 10000,
    get_backfill_tables_fn: Optional[
        Callable[[Path, Optional[Path]], Optional[Set[str]]]
    ] = None,
) -> CurrentStateResult:
    """Create complete current-state snapshot with merge and optional diff.

    Orchestrates the entire current-state creation process:
    1. Get table scope from transformed data
    2. Clear and prepare current-state directory
    3. Copy non-column entities (tables, schemas, databases)
    4. Merge columns (current + ancestral for unchanged tables)
    5. Create incremental diff (if previous state exists)
    6. Upload current-state and diff to S3

    This function encapsulates the complex orchestration logic that was
    previously in the write_current_state activity, making it reusable
    and testable independently.

    Args:
        connection_qualified_name: The connection qualified name.
        transformed_dir: Path to current run's transformed output
        previous_state_dir: Path to previous state (or None for first run)
        current_state_dir: Path where current state will be created
        s3_prefix: S3 prefix for persistent artifacts
        run_id: Workflow run ID for diff naming
        application_name: Optional application name override.
        copy_workers: Number of parallel workers for file operations
        column_chunk_size: Batch size for column processing
        get_backfill_tables_fn: Optional function to detect backfill tables

    Returns:
        CurrentStateResult with paths and statistics

    Raises:
        FileNotFoundError: If no tables found in transformed output

    Example:
        >>> result = await create_current_state_snapshot(
        ...     connection_qualified_name="default/oracle/1764230875",
        ...     transformed_dir=Path("./transformed"),
        ...     previous_state_dir=Path("./previous-state"),
        ...     current_state_dir=Path("./current-state"),
        ...     s3_prefix="persistent-artifacts/apps/oracle/conn/123",
        ...     run_id="abc123",
        ... )
        >>> print(f"Created {result.total_files} files")
    """
    current_state_s3_prefix = f"{s3_prefix}/current-state"
    table_scope = None
    diff_result = None
    incremental_diff_dir = None
    incremental_diff_s3_prefix = None

    with DuckDBConnectionManager() as conn_manager:
        conn = conn_manager.connection

        try:
            # Step 1: Get table scope (qualified names and incremental states)
            table_scope = get_current_table_scope(transformed_dir, conn=conn)
            if not table_scope or get_scope_length(table_scope) == 0:
                raise FileNotFoundError(
                    f"No tables found in transformed output: {transformed_dir}. "
                    "Cannot create current state without table metadata."
                )

            logger.info(
                "Creating current-state snapshot",
                table_count=get_scope_length(table_scope),
            )

            # Step 2: Clear and prepare current-state directory
            prepare_current_state_directory(current_state_dir)

            # Step 3: Copy non-column entities (tables, schemas, databases)
            copy_non_column_entities(
                transformed_dir=transformed_dir,
                current_state_dir=current_state_dir,
                copy_workers=copy_workers,
            )

            # Step 4: Merge columns (current + ancestral for NO CHANGE tables)
            merge_result, tables_with_columns = merge_ancestral_columns(
                current_transformed_dir=transformed_dir,
                previous_state_dir=previous_state_dir,
                new_state_dir=current_state_dir,
                table_scope=table_scope,
                column_chunk_size=column_chunk_size,
                conn=conn,
            )

            # Update table_scope with extracted columns info
            table_scope.tables_with_extracted_columns = tables_with_columns

            total_files = count_json_files_recursive(current_state_dir)

            logger.info(
                "Current-state merge complete",
                tables=get_scope_length(table_scope),
                columns_total=merge_result.columns_total,
                columns_from_current=merge_result.columns_from_current,
                columns_from_ancestral=merge_result.columns_from_ancestral,
                excluded=merge_result.excluded_already_extracted
                + merge_result.excluded_table_removed,
                total_files=total_files,
            )

            # Step 5: Create incremental-diff (only changed assets from this run)
            if previous_state_dir and previous_state_dir.exists():
                incremental_diff_subpath = INCREMENTAL_DIFF_SUBPATH_TEMPLATE.format(
                    run_id=run_id
                )
                incremental_diff_dir = get_persistent_artifacts_path(
                    connection_qualified_name,
                    incremental_diff_subpath,
                    application_name,
                )
                incremental_diff_s3_prefix = f"{s3_prefix}/{incremental_diff_subpath}"

                # Clear and recreate incremental-diff directory
                if incremental_diff_dir.exists():
                    shutil.rmtree(incremental_diff_dir)

                diff_result = create_incremental_diff(
                    transformed_dir=transformed_dir,
                    incremental_diff_dir=incremental_diff_dir,
                    table_scope=table_scope,
                    previous_state_dir=previous_state_dir,
                    conn=conn,
                    copy_workers=copy_workers,
                    get_backfill_tables_fn=get_backfill_tables_fn,
                )

                # Upload incremental-diff to S3
                await upload_prefix(
                    local_dir=str(incremental_diff_dir),
                    prefix=incremental_diff_s3_prefix,
                )
                logger.info(
                    "Incremental-diff uploaded to S3",
                    s3_prefix=incremental_diff_s3_prefix,
                )
            else:
                logger.info(
                    "Skipping incremental-diff creation "
                    "(first run - no previous state to diff against)"
                )

        finally:
            # Close the TableScope's disk-backed stores
            if table_scope:
                close_scope(table_scope)

    # Step 6: Upload current-state to S3
    await upload_prefix(
        local_dir=str(current_state_dir),
        prefix=current_state_s3_prefix,
    )
    logger.info("Current-state uploaded to S3: %s", current_state_s3_prefix)

    return CurrentStateResult(
        current_state_dir=current_state_dir,
        current_state_s3_prefix=current_state_s3_prefix,
        total_files=total_files,
        incremental_diff_dir=incremental_diff_dir,
        incremental_diff_s3_prefix=incremental_diff_s3_prefix,
        incremental_diff_files=diff_result.total_files if diff_result else 0,
    )
