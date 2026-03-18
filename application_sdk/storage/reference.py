"""FileReference persist / materialize operations.

A ``FileReference`` can be in one of two states:

* **Ephemeral** (``is_durable=False``): the data lives only on the local
  filesystem (``local_path`` is set).  This is safe to pass within a single
  activity, but cannot survive a Temporal payload round-trip because the
  remote worker won't have the file.

* **Durable** (``is_durable=True``): the data has been uploaded to the
  object store (``storage_path`` is set).  The reference can be serialised
  into a Temporal payload and materialised on any worker.

``persist_file_reference`` transitions ephemeral → durable.
``materialize_file_reference`` transitions durable → local (downloads the
file to a temp path when ``local_path`` is absent).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk.contracts.types import FileReference

if TYPE_CHECKING:
    from obstore.store import ObjectStore


def _make_storage_path(ref: FileReference) -> str:
    """Generate a unique storage path for a FileReference."""
    suffix = ""
    if ref.local_path:
        suffix = Path(ref.local_path).suffix
    return f"file_refs/{uuid.uuid4().hex}{suffix}"


async def persist_file_reference(
    store: "ObjectStore",
    ref: FileReference,
    *,
    key: str | None = None,
) -> FileReference:
    """Upload the local file referenced by *ref* to *store*.

    Args:
        store: Destination obstore store.
        ref: An ephemeral ``FileReference`` with ``local_path`` set.
        key: Override the generated storage path.

    Returns:
        A new durable ``FileReference`` (``is_durable=True``) pointing to
        the same data in the store.

    Raises:
        StorageError: If ``ref.local_path`` is ``None`` or the upload fails.
    """
    from application_sdk.storage.errors import StorageError
    from application_sdk.storage.ops import put

    if ref.is_durable:
        return ref  # already persisted — nothing to do

    if ref.local_path is None:
        raise StorageError(
            "Cannot persist FileReference: local_path is None",
            key=key,
        )

    storage_path = key or _make_storage_path(ref)

    with open(ref.local_path, "rb") as fh:
        data = fh.read()

    await put(storage_path, data, store)

    return FileReference(
        local_path=ref.local_path,
        is_durable=True,
        storage_path=storage_path,
    )


async def materialize_file_reference(
    store: "ObjectStore",
    ref: FileReference,
    *,
    local_dir: str | None = None,
) -> FileReference:
    """Download the file referenced by *ref* from *store* to a local temp path.

    If ``ref.local_path`` already exists on disk, returns *ref* unchanged.

    Args:
        store: Source obstore store.
        ref: A durable ``FileReference`` with ``storage_path`` set.
        local_dir: Optional directory for the temp file (uses the system
            temp dir if ``None``).

    Returns:
        A ``FileReference`` with ``local_path`` pointing to the downloaded
        file on the local filesystem.

    Raises:
        StorageNotFoundError: If the key does not exist in the store.
        StorageError: For other download failures.
    """
    from application_sdk.storage.errors import StorageNotFoundError
    from application_sdk.storage.ops import get_bytes

    if not ref.is_durable or ref.storage_path is None:
        return ref  # nothing to materialise

    if ref.local_path is not None and Path(ref.local_path).exists():
        return ref  # already materialised

    data = await get_bytes(ref.storage_path, store)
    if data is None:
        raise StorageNotFoundError(
            f"FileReference storage path not found in store: {ref.storage_path}",
            key=ref.storage_path,
        )

    suffix = Path(ref.storage_path).suffix or ""
    if local_dir:
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=local_dir)
    else:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)

    try:
        os.write(fd, data)
    finally:
        os.close(fd)

    return FileReference(
        local_path=tmp_path,
        is_durable=True,
        storage_path=ref.storage_path,
    )
