"""Current state writer utilities for incremental extraction.

This module provides helper functions for creating and uploading current state
snapshots during incremental metadata extraction workflows.

The state writer is responsible for:
1. Downloading transformed data from S3
2. Preparing current-state directory structure
3. Copying entity data (tables, schemas, databases, columns)
4. Creating incremental diffs for changed entities (including deletions)
5. Uploading the final snapshot to S3

Current-state is lightweight: it contains complete table/schema/database metadata,
but columns only for CREATED/UPDATED tables from the current extraction run.
Publish-cache serves as the single source of truth for what's published in Atlas.

Example workflow:
    1. download_transformed_data() - Get current run's transformed output
    2. prepare_previous_state() - Download previous state for comparison
    3. copy_entity_data() - Copy all entities (including columns) to current state
    4. Create incremental diff (with deletion detection)
    5. upload_current_state() - Upload to S3

High-level orchestration:
    Use create_current_state_snapshot() for complete state creation including
    diff generation in a single call.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.incremental.helpers import (
    copy_directory_parallel,
    count_json_files_recursive,
    download_s3_prefix_with_structure,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
)
from application_sdk.common.incremental.models import EntityType
from application_sdk.common.incremental.state.incremental_diff import (
    create_incremental_diff,
)
from application_sdk.common.incremental.state.table_scope import (
    close_scope,
    get_current_table_scope,
    get_scope_length,
    get_table_qns_from_columns,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.constants import (
    INCREMENTAL_DIFF_SUBPATH_TEMPLATE,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

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

    logger.info(f"Downloading transformed files from S3: {transformed_s3_prefix}")

    # Ensure local directory exists before download
    transformed_dir = Path(transformed_local_path)
    transformed_dir.mkdir(parents=True, exist_ok=True)

    await ObjectStore.download_prefix(
        source=transformed_s3_prefix,
        destination=str(transformed_dir),
        store_name=UPSTREAM_OBJECT_STORE_NAME,
    )

    return transformed_dir


async def prepare_previous_state(
    workflow_args: Dict[str, Any],
    current_state_available: bool,
    current_state_dir: Path,
) -> Optional[Path]:
    """Download previous state to a temporary location for comparison.

    When previous state exists, downloads it to a temporary directory
    to support ancestral column merging and incremental diff generation.

    Args:
        workflow_args: Workflow arguments containing connection info
        current_state_available: Whether previous state exists in S3
        current_state_dir: Path to current-state directory

    Returns:
        Path to temporary previous state directory, or None if no previous state

    Example:
        >>> prev_dir = await prepare_previous_state(args, True, current_state_dir)
        >>> if prev_dir:
        ...     # Use previous state for comparison
        ...     pass
    """
    if not current_state_available:
        return None

    s3_prefix = get_persistent_s3_prefix(workflow_args)
    current_state_s3_prefix = f"{s3_prefix}/current-state"

    previous_state_temp_dir = current_state_dir.parent.joinpath(
        f"{current_state_dir.name}.previous"
    )

    # Clean up any existing temp directory from previous failed runs
    if previous_state_temp_dir.exists():
        shutil.rmtree(previous_state_temp_dir)
    previous_state_temp_dir.mkdir(parents=True, exist_ok=True)

    # Download previous state from S3 to temporary location
    logger.info(f"Downloading previous state from S3: {current_state_s3_prefix}")
    try:
        await download_s3_prefix_with_structure(
            s3_prefix=current_state_s3_prefix,
            local_destination=previous_state_temp_dir,
        )
        logger.info(
            f"Previous state downloaded to temporary location: {previous_state_temp_dir}"
        )
        return previous_state_temp_dir
    except Exception as e:
        logger.error(f"Failed to download previous state: {e}")
        if previous_state_temp_dir.exists():
            shutil.rmtree(previous_state_temp_dir)
        raise


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
            logger.info(f"Copied {count} {entity_type.value} files to current state")

    return copy_counts


async def upload_current_state(
    current_state_dir: Path,
    workflow_args: Dict[str, Any],
) -> str:
    """Upload current-state snapshot to S3.

    Uploads the finalized current-state directory to S3 for persistence
    across workflow runs. This becomes the "previous state" for the next run.

    Args:
        current_state_dir: Path to local current-state directory
        workflow_args: Workflow arguments for S3 path resolution

    Returns:
        S3 prefix where current-state was uploaded

    Example:
        >>> s3_prefix = await upload_current_state(state_dir, workflow_args)
        >>> print(f"Uploaded to: {s3_prefix}")
    """
    s3_prefix = get_persistent_s3_prefix(workflow_args)
    current_state_s3_prefix = f"{s3_prefix}/current-state"

    await ObjectStore.upload_prefix(
        source=str(current_state_dir),
        destination=current_state_s3_prefix,
        store_name=UPSTREAM_OBJECT_STORE_NAME,
    )
    logger.info(f"Current-state uploaded to S3: {current_state_s3_prefix}")

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
                f"Cleaned up temporary previous state directory: {previous_state_dir}"
            )
        except Exception as e:
            # Non-critical cleanup failure - log warning but don't raise
            logger.warning(
                f"Failed to clean up temporary previous state directory: {e}"
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
    workflow_args: Dict[str, Any],
    transformed_dir: Path,
    previous_state_dir: Optional[Path],
    current_state_dir: Path,
    s3_prefix: str,
    run_id: str,
    copy_workers: int = 4,
    column_chunk_size: int = 10000,
    get_backfill_tables_fn: Optional[
        Callable[[Path, Optional[Path]], Optional[Set[str]]]
    ] = None,
) -> CurrentStateResult:
    """Create lightweight current-state snapshot and optional incremental diff.

    Orchestrates the entire current-state creation process:
    1. Get table scope from transformed data
    2. Clear and prepare current-state directory
    3. Copy all entities (tables, schemas, databases, columns)
    4. Create incremental diff with deletion detection (if previous state exists)
    5. Upload current-state and diff to S3

    Current-state is lightweight: it contains complete table/schema/database
    metadata, but columns only for CREATED/UPDATED tables. Publish-cache
    serves as the authoritative record of what's published in Atlas.

    Args:
        workflow_args: Workflow arguments for S3 path resolution
        transformed_dir: Path to current run's transformed output
        previous_state_dir: Path to previous state (or None for first run)
        current_state_dir: Path where current state will be created
        s3_prefix: S3 prefix for persistent artifacts
        run_id: Workflow run ID for diff naming
        copy_workers: Number of parallel workers for file operations
        column_chunk_size: Kept for backward compatibility (unused)
        get_backfill_tables_fn: Optional function to detect backfill tables

    Returns:
        CurrentStateResult with paths and statistics

    Raises:
        FileNotFoundError: If no tables found in transformed output

    Example:
        >>> result = await create_current_state_snapshot(
        ...     workflow_args=args,
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
                f"Creating current-state snapshot with {get_scope_length(table_scope)} tables"
            )

            # Step 2: Clear and prepare current-state directory
            prepare_current_state_directory(current_state_dir)

            # Step 3: Copy non-column entities (tables, schemas, databases)
            copy_non_column_entities(
                transformed_dir=transformed_dir,
                current_state_dir=current_state_dir,
                copy_workers=copy_workers,
            )

            # Step 4: Copy columns from transformed data (CREATED/UPDATED tables only)
            columns_copied = _copy_columns_from_transformed(
                transformed_dir=transformed_dir,
                current_state_dir=current_state_dir,
                copy_workers=copy_workers,
            )

            # Track which tables have extracted columns
            tables_with_columns: Set[str] = set()
            if columns_copied > 0:
                tables_with_columns = (
                    get_table_qns_from_columns(
                        current_state_dir.joinpath(EntityType.COLUMN.value),
                        conn=conn,
                    )
                    or set()
                )
            table_scope.tables_with_extracted_columns = tables_with_columns

            total_files = count_json_files_recursive(current_state_dir)

            logger.info(
                "Current-state snapshot complete: "
                "tables=%d, column_files=%d, "
                "tables_with_columns=%d, total_files=%d",
                get_scope_length(table_scope),
                columns_copied,
                len(tables_with_columns),
                total_files,
            )

            # Step 5: Create incremental-diff (only changed assets from this run)
            if previous_state_dir and previous_state_dir.exists():
                incremental_diff_subpath = INCREMENTAL_DIFF_SUBPATH_TEMPLATE.format(
                    run_id=run_id
                )
                incremental_diff_dir = get_persistent_artifacts_path(
                    workflow_args, incremental_diff_subpath
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
                await ObjectStore.upload_prefix(
                    source=str(incremental_diff_dir),
                    destination=incremental_diff_s3_prefix,
                    store_name=UPSTREAM_OBJECT_STORE_NAME,
                )
                logger.info(
                    f"Incremental-diff uploaded to S3: {incremental_diff_s3_prefix}"
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
    await ObjectStore.upload_prefix(
        source=str(current_state_dir),
        destination=current_state_s3_prefix,
        store_name=UPSTREAM_OBJECT_STORE_NAME,
    )
    logger.info(f"Current-state uploaded to S3: {current_state_s3_prefix}")

    return CurrentStateResult(
        current_state_dir=current_state_dir,
        current_state_s3_prefix=current_state_s3_prefix,
        total_files=total_files,
        incremental_diff_dir=incremental_diff_dir,
        incremental_diff_s3_prefix=incremental_diff_s3_prefix,
        incremental_diff_files=diff_result.total_files if diff_result else 0,
    )


def _copy_columns_from_transformed(
    transformed_dir: Path,
    current_state_dir: Path,
    copy_workers: int = 4,
) -> int:
    """Copy column files from transformed output to current-state.

    In the lightweight current-state model, columns are only stored for
    CREATED/UPDATED tables (i.e., whatever the transformer produced).
    No ancestral column merging is performed.

    Args:
        transformed_dir: Path to transformed output directory
        current_state_dir: Path to current-state directory
        copy_workers: Number of parallel workers for copy operations

    Returns:
        Number of column files copied
    """
    column_dir = transformed_dir.joinpath(EntityType.COLUMN.value)
    if not column_dir.exists():
        logger.info("No column directory in transformed output")
        return 0

    dest_dir = current_state_dir.joinpath(EntityType.COLUMN.value)
    count = copy_directory_parallel(column_dir, dest_dir, max_workers=copy_workers)
    logger.info("Copied %d column files to current state", count)
    return count
