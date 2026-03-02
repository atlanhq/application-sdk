"""Helper functions for incremental metadata extraction.

This module contains helper functions for:
- S3 path management for persistent artifacts
- Marker timestamp handling for incremental extraction
- Utility functions for file operations
"""

from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from application_sdk.common.incremental.models import IncrementalWorkflowArgs
from application_sdk.constants import (
    APPLICATION_NAME,
    MARKER_TIMESTAMP_FORMAT,
    PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE,
    TEMPORARY_PATH,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)


def extract_epoch_id_from_qualified_name(connection_qualified_name: str) -> str:
    """Extract the connection ID (epoch) from a connection qualified name.

    The connection qualified name follows the format: {tenant}/{connector}/{epoch}
    For example: "default/oracle/1764230875" -> "1764230875"

    This is used to create cleaner S3 paths for persistent artifacts like marker.txt
    and current-state folder, avoiding nested folder structures.

    Args:
        connection_qualified_name: The full qualified name (e.g., "default/oracle/1764230875")

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
            f"Could not extract epoch ID from connection_qualified_name: '{connection_qualified_name}'. "
            f"Expected format: 'tenant/connector/epoch'"
        )

    connection_id = parts[-1]

    if not connection_id.isdigit():
        logger.warning(
            f"Connection ID '{connection_id}' from '{connection_qualified_name}' "
            f"is not purely numeric. Using it anyway."
        )

    return connection_id


def get_persistent_s3_prefix(workflow_args: Dict[str, Any]) -> str:
    """Get the S3 key prefix for connection-scoped persistent artifacts.

    This prefix is used for storing marker.txt and current-state folder
    that persist across workflow runs for incremental extraction.

    Args:
        workflow_args: Dictionary containing workflow configuration.

    Returns:
        S3 key prefix like 'persistent-artifacts/apps/oracle/connection/1764230875'

    Raises:
        ValueError: If connection_qualified_name is not provided
    """
    args = IncrementalWorkflowArgs.model_validate(workflow_args)

    if not args.connection.connection_qualified_name:
        raise ValueError("connection_qualified_name is required")

    connection_id = extract_epoch_id_from_qualified_name(
        args.connection.connection_qualified_name
    )

    application_name = args.application_name or os.getenv(
        "ATLAN_APPLICATION_NAME", APPLICATION_NAME
    )

    s3_prefix = PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE.format(
        application_name=application_name,
        connection_id=connection_id,
    )

    logger.debug(
        f"S3 prefix for connection '{args.connection.connection_qualified_name}' -> '{s3_prefix}'"
    )
    return s3_prefix


def get_persistent_artifacts_path(
    workflow_args: Dict[str, Any], artifact_subpath: str
) -> Path:
    """Get local filesystem path for connection-scoped persistent artifacts.

    Args:
        workflow_args: Dictionary containing workflow configuration.
        artifact_subpath: Relative path under connection prefix.
            Examples:
            - "marker.txt" → connection-level marker
            - "current-state" → connection-level current state
            - "runs/{run_id}/incremental-diff" → run-specific incremental diff

    Returns:
        Local filesystem Path for the artifact.
    """
    s3_prefix = get_persistent_s3_prefix(workflow_args)
    return Path(TEMPORARY_PATH).joinpath(s3_prefix, artifact_subpath)


def normalize_marker_timestamp(marker: str) -> str:
    """Remove nanoseconds from marker timestamp (e.g., .123456789Z -> Z)."""
    normalized = re.sub(r"\.\d{1,9}(?=Z$)", "", marker)
    if normalized != marker:
        logger.info(f"Normalized marker: '{marker}' → '{normalized}'")
    return normalized


def prepone_marker_timestamp(marker: str, hours: int) -> str:
    """Move marker timestamp back by specified hours.

    This handles edge cases where objects created very close to the marker
    timestamp might be missed due to:
    - Clock skew between database and the extraction system
    - Database metadata update delays (CREATED/LAST_DDL_TIME propagation)
    - Transaction timing differences

    Args:
        marker: ISO 8601 timestamp string (e.g., '2025-01-15T10:30:00Z')
        hours: Number of hours to move the marker back

    Returns:
        Adjusted timestamp string in the same format

    Example:
        >>> prepone_marker_timestamp('2025-01-15T10:30:00Z', 3)
        '2025-01-15T07:30:00Z'
    """
    # Parse the timestamp
    dt = datetime.strptime(marker, MARKER_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)

    # Move back by specified hours
    adjusted = dt - timedelta(hours=hours)

    # Format back to string
    adjusted_str = adjusted.strftime(MARKER_TIMESTAMP_FORMAT)

    logger.info(f"Preponed marker by {hours}h: '{marker}' → '{adjusted_str}'")
    return adjusted_str


async def download_marker_from_s3(workflow_args: Dict[str, Any]) -> Optional[str]:
    """Download marker.txt from S3 and return its content, or None if not found.

    Args:
        workflow_args: Dictionary containing workflow configuration.

    Returns:
        Marker timestamp string if found, None otherwise
    """
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
    except FileNotFoundError:
        logger.info("Marker file not found in S3 (first incremental run)")
    except Exception as e:
        logger.warning(f"Failed to download marker from S3: {e}")
    return None


def is_incremental_run(workflow_args: Dict[str, Any]) -> bool:
    """Check if this is an incremental extraction run.

    Returns True only if:
    - incremental_extraction is enabled
    - marker_timestamp is present
    - current_state_available is True

    Args:
        workflow_args: Dictionary containing workflow configuration.

    Returns:
        True if all prerequisites for incremental extraction are met
    """
    args = IncrementalWorkflowArgs.model_validate(workflow_args)
    return args.is_incremental_ready()


async def download_s3_prefix_with_structure(
    s3_prefix: str,
    local_destination: Path,
    store_name: str = UPSTREAM_OBJECT_STORE_NAME,
) -> None:
    """Download files from S3 preserving relative directory structure.

    This helper handles path stripping correctly to maintain the expected
    directory structure locally.

    Args:
        s3_prefix: S3 prefix path to download from
        local_destination: Local directory to download files into
        store_name: Object store binding name

    Raises:
        Exception: If listing or downloading fails
    """
    # List files under the prefix from Object Store
    file_list = await ObjectStore.list_files(
        prefix=s3_prefix,
        store_name=store_name,
    )

    # Normalize source prefix for path stripping
    source_prefix = s3_prefix.rstrip("/")

    # Download each file with correct relative path
    for file_path in file_list:
        # Strip source prefix to get relative path
        if file_path.startswith(source_prefix):
            relative_path = file_path[len(source_prefix) :].lstrip("/")
        else:
            # If path doesn't start with prefix, use it as-is
            relative_path = file_path

        # Build local destination path
        local_file_path = local_destination.joinpath(relative_path)

        # Ensure parent directory exists
        local_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Download file from Object Store to local
        await ObjectStore.download_file(
            source=file_path,
            destination=str(local_file_path),
            store_name=store_name,
        )


# =============================================================================
# File Utilities
# =============================================================================


def count_json_files_recursive(directory: Path) -> int:
    """Recursively count JSON files without creating a list in memory.

    Args:
        directory: Directory to search recursively

    Returns:
        Number of JSON files
    """
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*.json"))


def copy_directory_parallel(
    src_dir: Path,
    dest_dir: Path,
    pattern: str = "*.json",
    max_workers: int = 3,
) -> int:
    """Copy files from source to destination directory in parallel.

    Args:
        src_dir: Source directory containing files to copy
        dest_dir: Destination directory (will be created if needed)
        pattern: Glob pattern for files to copy (default: ``*.json``)
        max_workers: Maximum number of parallel workers (default: 3)

    Returns:
        Number of files copied

    Raises:
        FileNotFoundError: If a source file disappears during copy
        PermissionError: If lacking permissions to read src or write dest
        OSError: For disk space issues or other I/O errors
    """
    if not src_dir.exists():
        return 0

    files = list(src_dir.glob(pattern))
    if not files:
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)

    def copy_single_file(src_file: Path) -> None:
        """Copy a single file to dest_dir. Raises on failure."""
        shutil.copy2(src_file, dest_dir / src_file.name)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(copy_single_file, files))

    return len(files)
