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
from typing import Optional

from application_sdk.constants import (
    APPLICATION_NAME,
    MARKER_TIMESTAMP_FORMAT,
    PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE,
    TEMPORARY_PATH,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.ops import download_file, download_prefix, list_keys, upload_file, upload_file_from_bytes, upload_prefix

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
            "Connection ID %s is not purely numeric (qn=%s), using it anyway",
            connection_id,
            connection_qualified_name,
        )

    return connection_id


def get_persistent_s3_prefix(
    connection_qualified_name: str,
    application_name: str = "",
) -> str:
    """Get the S3 key prefix for connection-scoped persistent artifacts.

    This prefix is used for storing marker.txt and current-state folder
    that persist across workflow runs for incremental extraction.

    Args:
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.

    Returns:
        S3 key prefix like 'persistent-artifacts/apps/oracle/connection/1764230875'

    Raises:
        ValueError: If connection_qualified_name is not provided
    """
    if not connection_qualified_name:
        raise ValueError("connection_qualified_name is required")

    connection_id = extract_epoch_id_from_qualified_name(connection_qualified_name)

    resolved_app_name = application_name or os.getenv(
        "ATLAN_APPLICATION_NAME", APPLICATION_NAME
    )

    s3_prefix = PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE.format(
        application_name=resolved_app_name,
        connection_id=connection_id,
    )

    logger.debug(
        "S3 prefix for connection %s: %s",
        connection_qualified_name,
        s3_prefix,
    )
    return s3_prefix


def get_persistent_artifacts_path(
    connection_qualified_name: str,
    artifact_subpath: str,
    application_name: str = "",
) -> Path:
    """Get local filesystem path for connection-scoped persistent artifacts.

    Args:
        connection_qualified_name: The connection qualified name.
        artifact_subpath: Relative path under connection prefix.
            Examples:
            - "marker.txt" → connection-level marker
            - "current-state" → connection-level current state
            - "runs/{run_id}/incremental-diff" → run-specific incremental diff
        application_name: Optional application name override.

    Returns:
        Local filesystem Path for the artifact.
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    return Path(TEMPORARY_PATH).joinpath(s3_prefix, artifact_subpath)


def normalize_marker_timestamp(marker: str) -> str:
    """Remove nanoseconds from marker timestamp (e.g., .123456789Z -> Z)."""
    normalized = re.sub(r"\.\d{1,9}(?=Z$)", "", marker)
    if normalized != marker:
        logger.info("Normalized marker: %s -> %s", marker, normalized)
    return normalized


def prepone_marker_timestamp(marker: str, hours: float) -> str:
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

    logger.info("Preponed marker by %.1f hours: %s -> %s", hours, marker, adjusted_str)
    return adjusted_str


async def download_marker_from_s3(
    connection_qualified_name: str,
    application_name: str = "",
) -> Optional[str]:
    """Download marker.txt from S3 and return its content, or None if not found.

    Args:
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.

    Returns:
        Marker timestamp string if found, None otherwise
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    marker_s3_key = f"{s3_prefix}/marker.txt"
    local_marker_path = get_persistent_artifacts_path(
        connection_qualified_name, "marker.txt", application_name
    )
    local_marker_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading marker from S3: %s", marker_s3_key)
    try:
        await download_file(
            key=marker_s3_key,
            local_path=str(local_marker_path),
        )
        if local_marker_path.exists() and local_marker_path.stat().st_size > 0:
            marker = local_marker_path.read_text(encoding="utf-8").strip()
            logger.info("Marker downloaded: %s", marker)
            return marker
        logger.info("Marker file downloaded but empty")
    except FileNotFoundError:
        logger.info("Marker file not found in S3 (first incremental run)")
    except Exception:
        logger.warning("Failed to download marker from S3", exc_info=True)
    return None


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
    file_list = await list_keys(
        prefix=s3_prefix,
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
        await download_file(
            key=file_path,
            local_path=str(local_file_path),
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
