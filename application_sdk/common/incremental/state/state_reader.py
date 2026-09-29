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
import warnings
from pathlib import Path
from typing import Tuple

# The module, not the name: a module-scope ``StorageError`` binding would
# resolve before ``__getattr__`` runs, and the deprecation would never fire.
import application_sdk.storage.errors as _storage_errors
from application_sdk._runtime.offload import run_in_thread
from application_sdk.common.incremental.helpers import (
    count_json_files_recursive,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import download_prefix

logger = get_logger(__name__)

#: name -> (replacement, why). This module no longer uses StorageError, which
#: made it importable from here by accident; it is served once more so imports
#: of it keep working, with a warning pointing at its home.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "StorageError": (
        "application_sdk.storage.errors.StorageError",
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
    return _storage_errors.StorageError


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

    Raises:
        StorageError: If the download fails. An absent state is not a failure
            (it downloads nothing and returns ``exists=False``); an object that
            vanishes between the listing and its fetch is, and raises
            ``StorageNotFoundError``.
        OSError: If the downloaded tree cannot be walked.

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

    # Clear and recreate local directory to prevent stale data from prior runs.
    # Offloaded: a prior run's current-state is one JSON file per asset, so this
    # tree scales with the connection and would stall the loop inline.
    if current_state_dir.exists():
        await run_in_thread(shutil.rmtree, current_state_dir)
    current_state_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading current-state folder from S3: %s", current_state_s3_prefix)

    # No handler for StorageNotFoundError. An absent prefix is not an error --
    # its listing is empty, so nothing is downloaded and the count below is 0,
    # which is the first-run answer. A not-found raised *here* means an object
    # the listing named vanished before its fetch: a failed state read, which
    # must propagate so the task retries rather than run a full extraction.
    # strip_prefix: current_state_dir already *is* the current-state
    # directory, so the store prefix must not be repeated inside it —
    # readers key off <current_state_dir>/table etc. (FND-340).
    await download_prefix(
        prefix=current_state_s3_prefix,
        local_dir=str(current_state_dir),
        strip_prefix=True,
    )

    # Offloaded: one JSON file per asset, so the walk scales with the
    # connection and would stall the loop (and the heartbeat) inline.
    json_count = await run_in_thread(count_json_files_recursive, current_state_dir)
    exists = json_count > 0

    if exists:
        logger.info("Current-state downloaded (%d JSON files)", json_count)
    else:
        logger.info(
            "Current-state not found or empty in S3 (prefix=%s) — first run",
            current_state_s3_prefix,
        )

    return current_state_dir, current_state_s3_prefix, exists, json_count
