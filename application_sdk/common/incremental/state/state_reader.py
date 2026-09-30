"""Current state reader utilities for incremental extraction.

Superseded by :class:`~application_sdk.common.incremental.state.store.CurrentStateStore`,
which answers "is there state?" with one listing and downloads only on demand,
into a run-scoped directory. ``download_current_state`` remains as a
deprecated shim over it.
"""

import warnings
from pathlib import Path
from typing import Tuple

from typing_extensions import deprecated

from application_sdk.common.incremental.helpers import get_persistent_artifacts_path
from application_sdk.common.incremental.state.store import CurrentStateStore
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: name -> (replacement, why). These names stopped being used here and were importable
#: from here only by accident; they are served once more so imports of them
#: keep working, with a warning pointing at their home.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "StorageError": (
        "application_sdk.storage.errors.StorageError",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "count_json_files_recursive": (
        "application_sdk.common.incremental.helpers.count_json_files_recursive",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "download_prefix": (
        "application_sdk.storage.batch.download_prefix",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "get_persistent_s3_prefix": (
        "application_sdk.common.incremental.helpers.get_persistent_s3_prefix",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "run_in_thread": (
        "application_sdk._runtime.offload.run_in_thread",
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
    "download_current_state is deprecated; use CurrentStateStore.probe() to "
    "learn whether state exists, and CurrentStateStore.materialize() into a "
    "run-scoped directory only when its files are needed — will be removed in "
    "v4.0.0."
)
async def download_current_state(
    connection_qualified_name: str,
    application_name: str = "",
) -> Tuple[Path, str, bool, int]:
    """Download the committed current-state snapshot to the connection's fixed directory.

    .. deprecated:: 3.x
        Use :meth:`CurrentStateStore.probe` and
        :meth:`CurrentStateStore.materialize`. The fixed per-connection
        directory this writes into is shared by every run of the connection on
        a worker. Will be removed in v4.0.0.

    Mirrors the committed snapshot (the manifest's keys, not every key under
    the prefix) into
    ``{TEMPORARY_PATH}/persistent-artifacts/apps/{app}/connection/{id}/current-state``.

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
        StorageError: If the listing or a download fails. An absent state is
            not a failure (it returns ``exists=False``).
        CurrentStateManifestError: If the manifest names missing keys.
    """
    store = CurrentStateStore.for_connection(
        connection_qualified_name, application_name
    )
    current_state_dir = get_persistent_artifacts_path(
        connection_qualified_name, "current-state", application_name
    )
    snapshot = await store.probe()
    await store.materialize(snapshot, current_state_dir)

    if snapshot.exists:
        logger.info("Current-state downloaded (%d JSON files)", snapshot.json_count)
    else:
        logger.info(
            "Current-state not found or empty in S3 (prefix=%s) — first run",
            store.s3_prefix,
        )
    return current_state_dir, store.s3_prefix, snapshot.exists, snapshot.json_count
