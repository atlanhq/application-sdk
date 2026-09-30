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

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from typing_extensions import deprecated

from application_sdk.common.atomic import atomic_write as _atomic_write
from application_sdk.common.atomic import disk_full_guard as _disk_full_guard
from application_sdk.common.incremental.helpers import download_marker_from_s3
from application_sdk.common.incremental.helpers import (
    get_persistent_artifacts_path as _get_persistent_artifacts_path,
)
from application_sdk.common.incremental.helpers import (
    get_persistent_s3_prefix,
    normalize_marker_timestamp,
    prepone_marker_timestamp,
)
from application_sdk.constants import MARKER_TIMESTAMP_FORMAT
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import upload_file_from_bytes
from application_sdk.storage.ops import upload_file as _upload_file

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.storage.ops import BoundStore

logger = get_logger(__name__)

# The marker is no longer written to a local file, so these stopped being
# imported here; they are kept as deprecated aliases for callers that imported
# (or patched) them via this module.


@deprecated(
    "atomic_write is deprecated here; use application_sdk.common.atomic.atomic_write, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
def atomic_write(
    path: str | Path,
    *,
    operation: str,
    mode: str = "wb",
    encoding: str | None = None,
    required_bytes: int | None = None,
    **open_kwargs: Any,
) -> AbstractContextManager[IO[Any]]:
    """Deprecated alias of :func:`application_sdk.common.atomic.atomic_write`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.common.atomic`. Will be removed in v4.0.0.
    """
    return _atomic_write(
        path,
        operation=operation,
        mode=mode,
        encoding=encoding,
        required_bytes=required_bytes,
        **open_kwargs,
    )


@deprecated(
    "disk_full_guard is deprecated here; use application_sdk.common.atomic.disk_full_guard, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
def disk_full_guard(
    path: str | Path, *, operation: str, required_bytes: int | None = None
) -> AbstractContextManager[None]:
    """Deprecated alias of :func:`application_sdk.common.atomic.disk_full_guard`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.common.atomic`. Will be removed in v4.0.0.
    """
    return _disk_full_guard(path, operation=operation, required_bytes=required_bytes)


@deprecated(
    "get_persistent_artifacts_path is deprecated here; use application_sdk.common.incremental.helpers.get_persistent_artifacts_path, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
def get_persistent_artifacts_path(
    connection_qualified_name: str, artifact_subpath: str, application_name: str = ""
) -> Path:
    """Deprecated alias of :func:`application_sdk.common.incremental.helpers.get_persistent_artifacts_path`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.common.incremental.helpers`. Will be removed in v4.0.0.
    """
    return _get_persistent_artifacts_path(
        connection_qualified_name, artifact_subpath, application_name
    )


@deprecated(
    "upload_file is deprecated here; use application_sdk.storage.ops.upload_file, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
async def upload_file(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    chunk_size: int | None = None,
    normalize: bool = True,
    retain_local_copy: bool = True,
    compute_hash: bool = True,
    verify: bool | None = None,
    write_sidecar: bool | None = None,
) -> str | None:
    """Deprecated alias of :func:`application_sdk.storage.ops.upload_file`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.storage.ops`. Will be removed in v4.0.0.
    """
    return await _upload_file(
        key,
        local_path,
        store,
        chunk_size=chunk_size,
        normalize=normalize,
        retain_local_copy=retain_local_copy,
        compute_hash=compute_hash,
        verify=verify,
        write_sidecar=write_sidecar,
    )


def create_next_marker() -> str:
    """Generate next marker timestamp for the current extraction run.

    Creates a UTC timestamp that will be persisted after successful extraction
    to mark the point from which the next incremental run should extract.

    Returns:
        Formatted timestamp string in ``YYYY-MM-DDTHH:MM:SSZ`` format.
    """
    return datetime.now(UTC).strftime(MARKER_TIMESTAMP_FORMAT)


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
        ...     "2024-01-15T10:00:00Z",
        ...     prepone_enabled=True,
        ...     prepone_hours=2
        ... )
        >>> # Returns "2024-01-15T08:00:00Z"
    """
    normalized = normalize_marker_timestamp(marker)

    if prepone_enabled and prepone_hours > 0:
        adjusted = prepone_marker_timestamp(normalized, prepone_hours)
        logger.info(
            "Marker preponed by %.1f hours: %s -> %s",
            prepone_hours,
            normalized,
            adjusted,
        )
        return adjusted

    return normalized


async def fetch_marker_from_storage(
    connection_qualified_name: str,
    application_name: str = "",
    existing_marker: str | None = None,
    prepone_enabled: bool = False,
    prepone_hours: float = 0,
) -> tuple[str | None, str]:
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
        logger.info("No marker found - full extraction (next=%s)", next_marker)
        return None, next_marker

    # Process the marker (normalize and optionally prepone)
    processed_marker = process_marker_timestamp(
        marker=marker,
        prepone_enabled=prepone_enabled,
        prepone_hours=prepone_hours,
    )

    logger.info(
        "Incremental extraction: marker=%s next=%s", processed_marker, next_marker
    )

    return processed_marker, next_marker


async def persist_marker_to_storage(
    connection_qualified_name: str,
    marker_value: str,
    application_name: str = "",
) -> dict[str, Any]:
    """Persist marker timestamp to S3 storage.

    Uploads the marker from memory for persistence across workflow runs.
    This marker will be used as the starting point for the next incremental
    extraction.

    Args:
        connection_qualified_name: The connection qualified name.
        marker_value: Marker timestamp string to persist
        application_name: Optional application name override.

    Returns:
        Dictionary with marker write details:
        - marker_written: True if successful
        - marker_timestamp: The persisted value
        - local_path: Always ``""`` (no local copy is written)
        - s3_key: S3 key where marker was uploaded

    Raises:
        MarkerUploadError: If the upload to S3 fails.

    Example:
        >>> result = await persist_marker_to_storage(
        ...     connection_qualified_name="default/oracle/1764230875",
        ...     marker_value="2024-01-15T10:30:45Z",
        ... )
        >>> print(f"Marker saved to: {result['s3_key']}")
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    marker_s3_key = f"{s3_prefix}/marker.txt"

    # Straight from memory: no per-connection local marker.txt for two runs
    # on one worker to share. upload_file_from_bytes stages through a private
    # temp file and writes the integrity sidecar, as the file upload did, so
    # a reader verifying against the sidecar never sees a stale digest.
    logger.info("Uploading marker to S3: %s", marker_s3_key)
    try:
        await upload_file_from_bytes(marker_s3_key, marker_value.encode("utf-8"))
        logger.info(
            "Marker uploaded to S3: key=%s value=%s", marker_s3_key, marker_value
        )
    # conformance: ignore[E004] broad catch wraps upload failure into typed MarkerUploadError and re-raises; no swallowing occurs
    except Exception as e:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            MarkerUploadError,
        )

        raise MarkerUploadError(cause=e) from e

    return {
        "marker_written": True,
        "marker_timestamp": marker_value,
        # Kept for callers that read the key; there is no local copy.
        "local_path": "",
        "s3_key": marker_s3_key,
    }
