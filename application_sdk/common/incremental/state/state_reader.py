"""Current state reader utilities for incremental extraction.

Superseded by :class:`~application_sdk.common.incremental.state.store.CurrentStateStore`,
which answers "is there state?" with one listing and downloads only on demand,
into a run-scoped directory. ``download_current_state`` remains as a
deprecated shim over it.
"""

import warnings
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Tuple, TypeVar

from typing_extensions import deprecated

from application_sdk._runtime.offload import run_in_thread as _run_in_thread
from application_sdk.common.incremental.helpers import (
    count_json_files_recursive as _count_json_files_recursive,
)
from application_sdk.common.incremental.helpers import get_persistent_artifacts_path
from application_sdk.common.incremental.helpers import (
    get_persistent_s3_prefix as _get_persistent_s3_prefix,
)
from application_sdk.common.incremental.state.store import CurrentStateStore
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import download_prefix as _download_prefix

if TYPE_CHECKING:
    from obstore.store import ObjectStore

_T = TypeVar("_T")

logger = get_logger(__name__)

#: name -> (replacement, why). These names stopped being used here and were importable
#: from here only by accident; they are served once more so imports of them
#: keep working, with a warning pointing at their home.
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
    module_name, _, attr = replacement.rpartition(".")
    from importlib import import_module  # noqa: PLC0415 — resolved on access only

    return getattr(import_module(module_name), attr)


@deprecated(
    "count_json_files_recursive is deprecated here; use application_sdk.common.incremental.helpers.count_json_files_recursive, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
def count_json_files_recursive(directory: Path) -> int:
    """Deprecated alias of :func:`application_sdk.common.incremental.helpers.count_json_files_recursive`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.common.incremental.helpers`. Will be removed in v4.0.0.
    """
    return _count_json_files_recursive(directory)


@deprecated(
    "download_prefix is deprecated here; use application_sdk.storage.batch.download_prefix, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
async def download_prefix(
    prefix: str,
    local_dir: "str | Path",
    store: "ObjectStore | None" = None,
    *,
    suffix: str = "",
    normalize: bool = True,
    strip_prefix: bool = False,
    max_concurrency: int = 4,
    sync: bool = False,
) -> List[str]:
    """Deprecated alias of :func:`application_sdk.storage.batch.download_prefix`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.storage.batch`. Will be removed in v4.0.0.
    """
    return await _download_prefix(
        prefix,
        local_dir,
        store,
        suffix=suffix,
        normalize=normalize,
        strip_prefix=strip_prefix,
        max_concurrency=max_concurrency,
        sync=sync,
    )


@deprecated(
    "get_persistent_s3_prefix is deprecated here; use application_sdk.common.incremental.helpers.get_persistent_s3_prefix, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
def get_persistent_s3_prefix(
    connection_qualified_name: str, application_name: str = ""
) -> str:
    """Deprecated alias of :func:`application_sdk.common.incremental.helpers.get_persistent_s3_prefix`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.common.incremental.helpers`. Will be removed in v4.0.0.
    """
    return _get_persistent_s3_prefix(connection_qualified_name, application_name)


@deprecated(
    "run_in_thread is deprecated here; use application_sdk._runtime.offload.run_in_thread, where it lives — "
    "it was only ever re-exported here by accident; will be removed in v4.0.0."
)
async def run_in_thread(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Deprecated alias of :func:`application_sdk._runtime.offload.run_in_thread`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk._runtime.offload`. Will be removed in v4.0.0.
    """
    return await _run_in_thread(func, *args, **kwargs)


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
