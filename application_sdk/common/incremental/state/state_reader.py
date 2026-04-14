"""Current state reader utilities for incremental extraction.

This module provides helper functions for downloading and managing the current state
from S3 during incremental metadata extraction workflows.

The current state represents the most recent snapshot of extracted metadata for a
connection, used for:
- Determining which tables have changed since the last extraction
- Providing ancestral column data for tables that haven't changed
- Supporting incremental diff generation
"""

import shutil
from pathlib import Path
from typing import Tuple

from application_sdk.common.incremental.helpers import (
    count_json_files_recursive,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
)
from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.ops import download_file, download_prefix, list_keys, upload_file, upload_file_from_bytes, upload_prefix

logger = get_logger(__name__)


async def download_current_state(
    connection_qualified_name: str,
    application_name: str = "",
) -> Tuple[Path, str, bool, int]:
    """Download current-state folder from S3 to local storage.

    Downloads the previous run's current-state snapshot from S3, which contains
    the metadata for all extracted entities. This is used for:
    1. Comparing with current extraction to detect changes
    2. Providing ancestral data for unchanged tables

    S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/

    Args:
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.

    Returns:
        Tuple containing:
            - current_state_dir: Path to local current-state directory
            - current_state_s3_prefix: S3 prefix for current-state
            - exists: Whether current state was successfully downloaded
            - json_count: Number of JSON files in the current state

    Example:
        >>> dir, prefix, exists, count = await download_current_state(
        ...     connection_qualified_name="default/oracle/1764230875"
        ... )
        >>> if exists:
        ...     print(f"Downloaded {count} files to {dir}")
        ... else:
        ...     print("First run - no previous state")
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    current_state_s3_prefix = f"{s3_prefix}/current-state"
    current_state_dir = get_persistent_artifacts_path(
        connection_qualified_name, "current-state", application_name
    )

    # Clear and recreate local directory to prevent stale data from prior runs
    if current_state_dir.exists():
        shutil.rmtree(current_state_dir)
    current_state_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading current-state folder from S3: %s", current_state_s3_prefix)

    exists = False
    json_count = 0

    try:
        await download_prefix(
            source=current_state_s3_prefix,
            destination=str(current_state_dir),
        )

        json_count = count_json_files_recursive(current_state_dir)
        exists = json_count > 0

        if exists:
            logger.info("Current-state downloaded (%d JSON files)", json_count)
        else:
            logger.info("Current-state downloaded but empty (no JSON files)")
    except Exception:
        # First run - current-state doesn't exist in S3 yet
        # This is expected behavior, not an error
        logger.info("Current-state not found in S3 (first run)")

    if not exists:
        logger.info("Current-state not available (first run or empty)")

    return current_state_dir, current_state_s3_prefix, exists, json_count
