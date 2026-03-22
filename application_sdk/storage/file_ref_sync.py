"""Transparent FileReference persist / materialise for Temporal activities.

These helpers are called automatically by ``create_activity_from_task()``
to handle large-payload cross-task data transfer:

* Before a task runs, ``materialize_file_refs`` downloads any durable
  ``FileReference`` fields in the input to local temp files.
* After a task completes, ``persist_file_refs`` uploads any ephemeral
  ``FileReference`` fields in the output to the store and marks them
  durable.

This makes ``FileReference`` round-trips transparent to task authors.

SHA-256 sidecar verification
-----------------------------
After every persist, a ``{storage_path}.sha256`` sidecar is written to the
store and a ``{local_path}.sha256`` sidecar is written locally.  Before a
re-use of an already-materialised file (``local_path`` is set), the local
sidecar is checked:

* **Missing sidecar** — conservative default: assume the file must be
  re-verified.  ``materialize_file_reference`` will fetch the stored sidecar
  and, if the hashes agree, simply stamp the local sidecar without re-downloading.
* **Sidecar mismatch** — file is corrupt or partially written; re-download.

This ensures that:
1. A Temporal retry on a different worker (stale ``local_path``) always
   triggers a fresh download.
2. A crash mid-download (partial file, no sidecar) is detected and recovered.
3. Once a sidecar exists and matches, re-downloads are skipped entirely.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from obstore.store import ObjectStore

logger = get_logger(__name__)


def _find_file_refs(data: Any) -> list[FileReference]:
    """Recursively find all FileReference instances in a BaseModel/dataclass tree."""
    if isinstance(data, FileReference):
        return [data]
    refs: list[FileReference] = []
    if isinstance(data, BaseModel):
        for name in type(data).model_fields:
            refs.extend(_find_file_refs(getattr(data, name)))
    elif isinstance(data, (list, tuple)):
        for item in data:
            refs.extend(_find_file_refs(item))
    elif isinstance(data, dict):
        for v in data.values():
            refs.extend(_find_file_refs(v))
    return refs


def _local_sidecar_ok(local_path: str) -> bool:
    """Return True if the local sha256 sidecar exists and matches the file.

    Conservative: any I/O error (missing file, missing sidecar, read error)
    returns False so the caller triggers a fresh materialize.

    Directories always return False — they always go through the full
    materialize check (list + download).
    """
    try:
        file_path = Path(local_path)
        if file_path.is_dir():
            return False
        sidecar_path = Path(local_path + ".sha256")
        if not file_path.exists() or not sidecar_path.exists():
            return False
        stored = sidecar_path.read_text().strip()
        h = hashlib.sha256()
        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
        return stored == actual
    except Exception:
        logger.warning("sha256 verification failed for local sidecar", exc_info=True)
        return False


def _needs_materialize(ref: FileReference) -> bool:
    """Return True if the durable ref needs to be downloaded (or re-verified).

    A durable ref needs materializing when any of the following is true:

    * ``local_path`` is ``None`` — file has never been downloaded.
    * ``local_path`` is set but the file no longer exists on disk
      (e.g. the activity retried on a different worker pod).
    * The local ``.sha256`` sidecar is missing — conservative default;
      ``materialize_file_reference`` will validate against the stored sidecar
      before deciding whether to skip the actual download.
    * The local sidecar exists but does not match the file's current sha256 —
      the file is corrupt or was only partially written.
    """
    if not ref.is_durable or ref.storage_path is None:
        return False
    if ref.local_path is None:
        return True
    # File missing or sidecar check fails → needs materialize
    return not _local_sidecar_ok(ref.local_path)


def has_refs_to_persist(data: Any) -> bool:
    """Return True if *data* contains ephemeral FileReferences (local but not durable)."""
    return any(
        ref.local_path is not None and not ref.is_durable
        for ref in _find_file_refs(data)
    )


def has_refs_to_materialize(data: Any) -> bool:
    """Return True if *data* contains durable FileReferences that need downloading.

    Checks for: missing local file, stale local_path (different worker),
    missing local sha256 sidecar (conservative), and sidecar hash mismatch
    (corrupt/partial file).
    """
    return any(_needs_materialize(ref) for ref in _find_file_refs(data))


async def _replace_refs(
    data: Any, store: "ObjectStore", mode: str, output_path: str | None = None
) -> Any:
    """Recursively replace FileReference instances in a dataclass tree.

    Args:
        data: Input dataclass, list, tuple, dict, or scalar.
        store: obstore store used for upload/download.
        mode: ``"persist"`` or ``"materialize"``.
        output_path: Run-scoped base prefix passed through to
            ``persist_file_reference`` for ``RETAINED``-tier refs.

    Returns:
        A new object tree with replaced FileReference instances (original
        objects are never mutated).
    """
    from application_sdk.storage.reference import (
        materialize_file_reference,
        persist_file_reference,
    )

    if isinstance(data, FileReference):
        if mode == "persist" and data.local_path is not None and not data.is_durable:
            return await persist_file_reference(store, data, output_path=output_path)
        if mode == "materialize" and _needs_materialize(data):
            return await materialize_file_reference(store, data)
        return data

    if isinstance(data, BaseModel):
        changes: dict[str, Any] = {}
        for name in type(data).model_fields:
            old_val = getattr(data, name)
            new_val = await _replace_refs(old_val, store, mode, output_path=output_path)
            if new_val is not old_val:
                changes[name] = new_val
        return data.model_copy(update=changes) if changes else data

    if isinstance(data, list):
        new_list = [
            await _replace_refs(item, store, mode, output_path=output_path)
            for item in data
        ]
        return new_list if any(n is not o for n, o in zip(new_list, data)) else data

    if isinstance(data, tuple):
        new_tuple = tuple(
            await _replace_refs(item, store, mode, output_path=output_path)
            for item in data
        )
        return new_tuple if new_tuple != data else data

    return data


async def persist_file_refs(
    store: "ObjectStore", data: Any, output_path: str | None = None
) -> Any:
    """Upload all ephemeral FileReferences in *data* to the store.

    Args:
        store: Destination obstore store.
        data: Dataclass tree potentially containing ephemeral
            ``FileReference`` objects.
        output_path: Run-scoped base prefix (e.g.
            ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}``).  Required
            for any ``RETAINED``-tier ``FileReference`` in *data*.

    Returns:
        New object tree with all ephemeral FileReferences replaced by
        durable ones (with sha256 sidecars written).
    """
    return await _replace_refs(data, store, "persist", output_path=output_path)


async def materialize_file_refs(store: "ObjectStore", data: Any) -> Any:
    """Download all durable FileReferences in *data* to local temp files.

    Handles:
    * Normal case: ``local_path`` is ``None`` → download + write sidecar.
    * Retry on different worker: ``local_path`` set but file gone → download.
    * Missing sidecar: file exists but no ``.sha256`` sidecar → validate
      against stored sidecar; skip download if hashes agree.
    * Corrupt file: sidecar mismatch → re-download.

    Returns:
        New object tree with all durable FileReferences replaced by local
        ones (``local_path`` set, ``.sha256`` sidecar written).
    """
    return await _replace_refs(data, store, "materialize")
