"""Incremental extraction marker management.

This module provides helper functions for managing incremental extraction markers.
Markers are timestamps stored in S3 that track the last successful extraction,
enabling subsequent runs to extract only changed data.

Marker workflow:
1. fetch_marker_from_storage() - Download and validate existing marker
2. create_next_marker() - Generate next marker timestamp for current run
3. persist_marker_to_storage() - Upload marker after successful extraction

S3 Path Structure:
    persistent-artifacts/apps/{application_name}/connection/{connection_id}/marker.txt

Example:
    >>> marker, next_marker = await fetch_marker_from_storage(
    ...     connection_qualified_name="default/oracle/1764230875"
    ... )
    >>> # ... perform extraction with marker filter ...
    >>> await persist_marker_to_storage(
    ...     connection_qualified_name="default/oracle/1764230875",
    ...     marker_value=next_marker,
    ... )
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from application_sdk.common.incremental.helpers import (
    download_marker_from_s3,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
    normalize_marker_timestamp,
    prepone_marker_timestamp,
)
from application_sdk.constants import (
    MARKER_TIMESTAMP_FORMAT,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)


def create_next_marker() -> str:
    """Generate next marker timestamp for the current extraction run.

    Creates a UTC timestamp that will be persisted after successful extraction
    to mark the point from which the next incremental run should extract.

    Returns:
        Formatted timestamp string in ``YYYY-MM-DDTHH:MM:SSZ`` format.
    """
    return datetime.now(timezone.utc).strftime(MARKER_TIMESTAMP_FORMAT)


def process_marker_timestamp(
    marker: str,
    prepone_enabled: bool = False,
    prepone_hours: float = 0,
) -> str:
    """Process and optionally prepone a marker timestamp.

    Normalizes the marker format and optionally moves it back in time
    to catch any edge cases (transactions that started before but
    committed after the marker was set).

    Args:
        marker: Raw marker timestamp string
        prepone_enabled: Whether to prepone the marker
        prepone_hours: Number of hours to prepone (move back in time)

    Returns:
        Processed marker timestamp string

    Example:
        >>> processed = process_marker_timestamp(
        ...     "2024-01-15 10:00:00",
        ...     prepone_enabled=True,
        ...     prepone_hours=2
        ... )
        >>> # Returns "2024-01-15 08:00:00"
    """
    normalized = normalize_marker_timestamp(marker)

    if prepone_enabled and prepone_hours > 0:
        adjusted = prepone_marker_timestamp(normalized, prepone_hours)
        logger.info(
            f"Marker preponed: original={normalized}, adjusted={adjusted} "
            f"(preponed by {prepone_hours}h)"
        )
        return adjusted

    return normalized


async def fetch_marker_from_storage(
    connection_qualified_name: str,
    application_name: str = "",
    existing_marker: Optional[str] = None,
    prepone_enabled: bool = False,
    prepone_hours: float = 0,
) -> Tuple[Optional[str], str]:
    """Fetch and process the incremental marker from storage.

    Attempts to retrieve an existing marker from:
    1. existing_marker parameter (if provided directly)
    2. S3 persistent storage (from previous successful run)

    Also creates the next_marker timestamp for the current run.

    Args:
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.
        existing_marker: Pre-existing marker value (e.g., from manual override).
        prepone_enabled: Whether to prepone the marker timestamp
        prepone_hours: Hours to prepone (move marker back in time)

    Returns:
        Tuple of (processed_marker, next_marker):
        - processed_marker: Processed existing marker or None if first run
        - next_marker: New timestamp for current run

    Example:
        >>> marker, next_marker = await fetch_marker_from_storage(
        ...     connection_qualified_name="default/oracle/1764230875"
        ... )
        >>> if marker:
        ...     print(f"Incremental from: {marker}")
        ... else:
        ...     print("Full extraction (first run)")
    """
    next_marker = create_next_marker()

    marker = existing_marker

    if not marker:
        # Try to download from S3
        marker = await download_marker_from_s3(
            connection_qualified_name, application_name
        )

    if not marker:
        logger.info(f"No marker found - full extraction (next_marker={next_marker})")
        return None, next_marker

    # Process the marker (normalize and optionally prepone)
    processed_marker = process_marker_timestamp(
        marker=marker,
        prepone_enabled=prepone_enabled,
        prepone_hours=prepone_hours,
    )

    logger.info(
        f"Incremental extraction: marker={processed_marker}, next={next_marker}"
    )

    return processed_marker, next_marker


async def persist_marker_to_storage(
    connection_qualified_name: str,
    marker_value: str,
    application_name: str = "",
) -> Dict[str, Any]:
    """Persist marker timestamp to S3 storage.

    Writes the marker to both local storage and S3 for persistence
    across workflow runs. This marker will be used as the starting
    point for the next incremental extraction.

    Args:
        connection_qualified_name: The connection qualified name.
        marker_value: Marker timestamp string to persist
        application_name: Optional application name override.

    Returns:
        Dictionary with marker write details:
        - marker_written: True if successful
        - marker_timestamp: The persisted value
        - local_path: Path to local marker file
        - s3_key: S3 key where marker was uploaded

    Raises:
        Exception: If upload to S3 fails

    Example:
        >>> result = await persist_marker_to_storage(
        ...     connection_qualified_name="default/oracle/1764230875",
        ...     marker_value="2024-01-15T10:30:45Z",
        ... )
        >>> print(f"Marker saved to: {result['s3_key']}")
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    marker_s3_key = f"{s3_prefix}/marker.txt"
    local_marker_path = get_persistent_artifacts_path(
        connection_qualified_name, "marker.txt", application_name
    )

    # Ensure local directory exists
    local_marker_path.parent.mkdir(parents=True, exist_ok=True)

    # Write marker to local file
    logger.info(f"Writing marker to local file: {local_marker_path}")
    local_marker_path.write_text(marker_value, encoding="utf-8")

    # Upload marker to S3
    logger.info(f"Uploading marker to S3: {marker_s3_key}")
    try:
        await ObjectStore.upload_file(
            source=str(local_marker_path),
            destination=marker_s3_key,
            store_name=UPSTREAM_OBJECT_STORE_NAME,
            retain_local_copy=True,
        )
        logger.info(f"Marker uploaded to S3: {marker_s3_key} → {marker_value}")
    except Exception as e:
        logger.error(f"Failed to upload marker to S3: {e}")
        raise

    return {
        "marker_written": True,
        "marker_timestamp": marker_value,
        "local_path": str(local_marker_path),
        "s3_key": marker_s3_key,
    }
