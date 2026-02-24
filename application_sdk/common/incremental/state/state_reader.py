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
from typing import Any, Dict, Tuple

from application_sdk.common.incremental.helpers import (
    count_json_files_recursive,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
)
from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)


async def download_current_state(
    workflow_args: Dict[str, Any],
) -> Tuple[Path, str, bool, int]:
    """Download current-state folder from S3 to local storage.

    Downloads the previous run's current-state snapshot from S3, which contains
    the metadata for all extracted entities. This is used for:
    1. Comparing with current extraction to detect changes
    2. Providing ancestral data for unchanged tables

    S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/

    Args:
        workflow_args: Dictionary containing workflow configuration with:
            - connection.connection_qualified_name: Connection identifier
            - Other workflow metadata

    Returns:
        Tuple containing:
            - current_state_dir: Path to local current-state directory
            - current_state_s3_prefix: S3 prefix for current-state
            - exists: Whether current state was successfully downloaded
            - json_count: Number of JSON files in the current state

    Example:
        >>> dir, prefix, exists, count = await download_current_state(workflow_args)
        >>> if exists:
        ...     print(f"Downloaded {count} files to {dir}")
        ... else:
        ...     print("First run - no previous state")
    """
    s3_prefix = get_persistent_s3_prefix(workflow_args)
    current_state_s3_prefix = f"{s3_prefix}/current-state"
    current_state_dir = get_persistent_artifacts_path(workflow_args, "current-state")

    # Clear and recreate local directory to prevent stale data from prior runs
    if current_state_dir.exists():
        shutil.rmtree(current_state_dir)
    current_state_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading current-state folder from S3: {current_state_s3_prefix}")

    exists = False
    json_count = 0

    try:
        await ObjectStore.download_prefix(
            source=current_state_s3_prefix,
            destination=str(current_state_dir),
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )

        json_count = count_json_files_recursive(current_state_dir)
        exists = json_count > 0

        if exists:
            logger.info(f"Current-state downloaded with {json_count} JSON files")
        else:
            logger.info("Current-state downloaded but empty (no JSON files)")
    except Exception as e:
        # First run - current-state doesn't exist in S3 yet
        # This is expected behavior, not an error
        logger.info(f"Current-state not found in S3 (first run): {e}")

    if not exists:
        logger.info("Current-state not available (first run or empty)")

    return current_state_dir, current_state_s3_prefix, exists, json_count
