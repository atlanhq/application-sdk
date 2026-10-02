"""Helper functions for incremental metadata extraction.

This module contains helper functions for:
- S3 path management for persistent artifacts
- Marker timestamp handling for incremental extraction
- Utility functions for file operations
"""

from __future__ import annotations

import hashlib
import os
import re
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from typing_extensions import deprecated

from application_sdk.common._listing import safe_list_directory
from application_sdk.common.atomic import (
    atomic_copy,
    disk_full_guard,
    ensure_free_space,
)
from application_sdk.constants import (
    APPLICATION_NAME,
    MARKER_FILENAME,
    MARKER_TIMESTAMP_FORMAT,
    MAX_CONCURRENT_STORAGE_TRANSFERS,
    PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE,
    TEMPORARY_PATH,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import download_prefix
from application_sdk.storage.integrity import (
    check_transfer_digest,
    read_expected_digest,
    verification_enabled,
)
from application_sdk.storage.ops import _get_bytes
from application_sdk.storage.ops import download_file as _download_file
from application_sdk.storage.ops import normalize_key

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.storage.ops import BoundStore

logger = get_logger(__name__)

#: name -> (replacement, why). The marker read no longer downloads to a local file, so
#: these stopped being imported here; they are served once more for callers
#: that imported (or patched) them via this module.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "StorageNotFoundError": (
        "application_sdk.storage.errors.StorageNotFoundError",
        "it was only ever re-exported here as a side effect of an import",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the removed re-exports once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated here; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    module_name, _, attr = replacement.rpartition(".")
    from importlib import import_module  # noqa: PLC0415 — resolved on access only

    return getattr(import_module(module_name), attr)


@deprecated(
    "download_file is deprecated here; use application_sdk.storage.ops.download_file, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
async def download_file(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    compute_hash: bool = False,
    min_chunk_size: int = 10 * 1024 * 1024,
    normalize: bool = True,
    verify: bool | None = None,
    expected_sha256: str | None = None,
    sidecar_present: bool | None = None,
) -> str | None:
    """Deprecated alias of :func:`application_sdk.storage.ops.download_file`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.storage.ops`. Will be removed in
        v4.0.0.
    """
    return await _download_file(
        key,
        local_path,
        store,
        compute_hash=compute_hash,
        min_chunk_size=min_chunk_size,
        normalize=normalize,
        verify=verify,
        expected_sha256=expected_sha256,
        sidecar_present=sidecar_present,
    )


def extract_epoch_id_from_qualified_name(connection_qualified_name: str) -> str:
    """Extract the connection ID (epoch) from a connection qualified name.

    The connection qualified name follows the format: {tenant}/{connector}/{epoch}
    For example: "default/oracle/1764230875" -> "1764230875"

    This is used to create cleaner S3 paths for persistent artifacts like marker.txt
    and current-state folder, avoiding nested folder structures.

    A last segment that is not a numeric epoch is accepted with a warning, not
    rejected: connections named after a workflow or environment
    ("default/oracle/some-name") are legitimate — a tenant that provisions
    connections programmatically produces them — and they crawl normally. Any
    caller that raises on such a name is stricter than this function for no
    gain, which is how CONNECT-1136 broke a tenant's miner while its crawler
    ran fine. Route through this function rather than re-deriving the segment.

    An *empty* last segment is the one case that is rejected, because it is not
    a name at all: it collapses every such connection onto one directory, so
    they would share a marker and silently overwrite each other's watermark.
    Failing is strictly better than that.

    Args:
        connection_qualified_name: The full qualified name (e.g., "default/oracle/1764230875")

    Returns:
        The connection ID (epoch number) as a string

    Raises:
        ConnectionQualifiedNameEmptyError: If the qualified name is empty.
        ConnectionQualifiedNameFormatError: If the qualified name has fewer than
            three segments, or its last segment is empty.
    """
    if not connection_qualified_name:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            ConnectionQualifiedNameEmptyError,
        )

        raise ConnectionQualifiedNameEmptyError()

    parts = connection_qualified_name.split("/")

    if len(parts) < 3:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            ConnectionQualifiedNameFormatError,
        )

        raise ConnectionQualifiedNameFormatError()

    connection_id = parts[-1]

    # A trailing slash ("default/oracle/") passes the segment-count check above
    # but yields an empty connection directory — ".../connection/" — which every
    # connection ending that way would share. Two connections writing one marker
    # move each other's watermark, so each re-extracts from the other's window or
    # skips its own: silent, and only visible as missing data much later. Reject
    # it here, at the one place every caller derives the segment.
    if not connection_id:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            ConnectionQualifiedNameFormatError,
        )

        raise ConnectionQualifiedNameFormatError(
            message=(
                "connection_qualified_name has an empty last segment "
                f"(qn={connection_qualified_name!r}); it would share a persistent "
                "artifacts directory with every other such connection"
            ),
        )

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
        ConnectionQualifiedNameMissingError: If connection_qualified_name is not provided.
    """
    if not connection_qualified_name:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            ConnectionQualifiedNameMissingError,
        )

        raise ConnectionQualifiedNameMissingError()

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
    dt = datetime.strptime(marker, MARKER_TIMESTAMP_FORMAT).replace(tzinfo=UTC)

    # Move back by specified hours
    adjusted = dt - timedelta(hours=hours)

    # Format back to string
    adjusted_str = adjusted.strftime(MARKER_TIMESTAMP_FORMAT)

    logger.info("Preponed marker by %.1f hours: %s -> %s", hours, marker, adjusted_str)
    return adjusted_str


async def download_marker_from_s3(
    connection_qualified_name: str,
    application_name: str = "",
) -> str | None:
    """Read marker.txt from S3 into memory and return it, or None if not found.

    Nothing is written locally: the marker is a few bytes, and a local copy at
    a per-connection path is one more file two runs on a worker would share.

    Args:
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.

    Returns:
        Marker timestamp string if found, None otherwise

    Raises:
        StorageError: If the read fails for any reason other than the marker
            not existing. A missing marker means "first run"; any other
            failure must not silently become a full extraction, so it
            propagates and the task retries.
        StorageIntegrityError: If the marker does not match its ``.sha256``
            sidecar (only when transfer verification is on).
    """
    s3_prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
    marker_s3_key = f"{s3_prefix}/{MARKER_FILENAME}"

    logger.info("Reading marker from S3: %s", marker_s3_key)
    raw = await _get_bytes(marker_s3_key)
    if raw is None:
        logger.info("Marker file not found in S3 (first incremental run)")
        return None
    await _verify_marker_bytes(marker_s3_key, raw)
    marker = raw.decode("utf-8").strip()
    if not marker:
        logger.info("Marker file found but empty")
        return None
    logger.info("Marker read: %s", marker)
    return marker


async def _verify_marker_bytes(marker_s3_key: str, raw: bytes) -> None:
    """Check *raw* against the marker's ``.sha256`` sidecar, as a download would.

    The in-memory read skips the transfer checks ``download_file`` makes, and a
    damaged marker that still decodes can carry a later timestamp, which would
    make the next extraction skip changes. No sidecar (or verification turned
    off) means nothing to verify against, exactly as for a download.

    Raises:
        StorageIntegrityError: If the bytes do not match the recorded digest.
    """
    if not verification_enabled(None):
        return
    key = normalize_key(marker_s3_key)
    expected = await read_expected_digest(None, key)
    if expected is None:
        return
    check_transfer_digest(
        "marker read",
        key,
        expected=expected,
        actual=hashlib.sha256(raw).hexdigest(),
    )


async def download_s3_prefix_with_structure(
    s3_prefix: str,
    local_destination: Path,
    max_concurrency: int = MAX_CONCURRENT_STORAGE_TRANSFERS,
) -> None:
    """Download files under *s3_prefix* into *local_destination*, prefix stripped.

    Supported alias for :func:`~application_sdk.storage.batch.download_prefix`
    with ``strip_prefix=True``: ``<s3_prefix>/table/chunk-0.json`` lands at
    ``<local_destination>/table/chunk-0.json``.

    This is a **supported** compatibility alias, not a deprecated one — it is
    retained indefinitely because app code outside the SDK imports it, and there
    is no removal target. New SDK call sites should prefer ``download_prefix``
    directly so the incremental path keeps one download implementation and one
    path policy (FND-340).

    Args:
        s3_prefix: Object-store prefix to download from.
        local_destination: Local directory to download files into.
        max_concurrency: Maximum number of concurrent downloads
            (default: ``MAX_CONCURRENT_STORAGE_TRANSFERS``).

    Raises:
        StorageError: If listing or downloading fails.
        StorageIntegrityError: If an object does not match its sidecar digest.
    """
    await download_prefix(
        prefix=s3_prefix,
        local_dir=local_destination,
        strip_prefix=True,
        max_concurrency=max_concurrency,
    )


# =============================================================================
# File Utilities
# =============================================================================


def count_json_files_recursive(directory: Path) -> int:
    """Recursively count JSON files under *directory*.

    Walks with :func:`~application_sdk.common._listing.safe_list_directory`
    rather than ``Path.rglob``, which silently swallows an ``OSError`` part-way
    through a walk and so under-counts instead of failing. Blocking: async
    callers must offload it with ``run_in_thread``.

    Args:
        directory: Directory to search recursively

    Returns:
        Number of JSON files; 0 when *directory* does not exist.

    Raises:
        OSError: If the tree cannot be walked.
    """
    if not directory.exists():
        return 0
    return sum(1 for p in safe_list_directory(directory) if p.suffix == ".json")


def copy_directory_parallel(
    src_dir: Path,
    dest_dir: Path,
    pattern: str = "*.json",
    max_workers: int = 3,
) -> int:
    """Copy files from source to destination directory.

    Copies one file at a time on the calling thread. Every caller already runs
    this inside ``run_in_thread``, and a thread pool opened from an offloaded
    thread is the nesting ``_runtime/offload.py`` warns against: it multiplies
    the process's thread count without the offload layer seeing it.

    Args:
        src_dir: Source directory containing files to copy
        dest_dir: Destination directory (will be created if needed)
        pattern: Glob pattern for files to copy (default: ``*.json``)
        max_workers: Ignored; the copy is sequential. Kept so existing callers
            that pass it keep working.

    Returns:
        Number of files copied

    Raises:
        DiskFullError: If the destination filesystem cannot hold the copy —
            either because it plainly has less room than the whole batch needs,
            or because it ran out part-way through.
        FileNotFoundError: If a source file disappears during copy
        PermissionError: If lacking permissions to read src or write dest
        OSError: For other I/O errors
    """
    if not src_dir.exists():
        return 0

    files = list(src_dir.glob(pattern))
    if not files:
        return 0

    # Inside the guard: creating the destination is itself a write, and on a
    # full filesystem it fails with the same ENOSPC/EDQUOT the copies below
    # would have — the docstring promises DiskFullError for exactly this.
    with disk_full_guard(dest_dir, operation="carry-forward copy"):
        dest_dir.mkdir(parents=True, exist_ok=True)

    # This is the carry-forward copy behind FND-318. It ran out of space
    # part-way through and `shutil.copy2` left a truncated file at the
    # artifact's real name; the run then carried it forward and uploaded it,
    # and a publish forty minutes later failed in DuckDB at the same byte
    # offset on every retry. Two changes close that: the batch's total size is
    # checked before the first byte moves, so a plainly-undersized volume fails
    # in seconds with a number an operator can act on; and each file is staged
    # and renamed, so a failure part-way through leaves no file at all rather
    # than a partial one.
    total_bytes = 0
    for src_file in files:
        try:
            total_bytes += src_file.stat().st_size
        except OSError:
            # Only the estimate is affected — this file is still copied below,
            # and that copy raises its own error if the file is really gone.
            # DEBUG because a file disappearing between the glob and the stat
            # is a benign race, not a condition anyone acts on.
            logger.debug(
                "Could not size %s for the carry-forward space check; the "
                "estimate will be low by that file",
                src_file,
                exc_info=True,
            )
            continue
    ensure_free_space(dest_dir, total_bytes, operation="carry-forward copy")

    for src_file in files:
        atomic_copy(src_file, dest_dir / src_file.name, operation="carry-forward copy")

    return len(files)
