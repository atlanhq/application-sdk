"""Transparent FileReference persist / materialise for Temporal activities.

These helpers are called automatically by ``create_activity_from_task()``
to handle large-payload cross-task data transfer:

* Before a task runs, ``materialize_file_refs`` downloads any durable
  ``FileReference`` fields in the input to local temp files.
* After a task completes, ``persist_file_refs`` uploads any ephemeral
  ``FileReference`` fields in the output to the store and marks them
  durable.

This makes ``FileReference`` round-trips transparent to task authors.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from application_sdk.contracts.types import FileReference

if TYPE_CHECKING:
    from obstore.store import ObjectStore


def _find_file_refs(data: Any) -> list[FileReference]:
    """Recursively find all FileReference instances in a dataclass tree."""
    if isinstance(data, FileReference):
        return [data]
    refs: list[FileReference] = []
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        for f in dataclasses.fields(data):
            refs.extend(_find_file_refs(getattr(data, f.name)))
    elif isinstance(data, (list, tuple)):
        for item in data:
            refs.extend(_find_file_refs(item))
    elif isinstance(data, dict):
        for v in data.values():
            refs.extend(_find_file_refs(v))
    return refs


def has_refs_to_persist(data: Any) -> bool:
    """Return True if *data* contains ephemeral FileReferences (local but not durable)."""
    return any(
        ref.local_path is not None and not ref.is_durable
        for ref in _find_file_refs(data)
    )


def has_refs_to_materialize(data: Any) -> bool:
    """Return True if *data* contains durable FileReferences that need downloading."""
    return any(
        ref.is_durable and ref.storage_path is not None and ref.local_path is None
        for ref in _find_file_refs(data)
    )


async def _replace_refs(data: Any, store: "ObjectStore", mode: str) -> Any:
    """Recursively replace FileReference instances in a dataclass tree.

    Args:
        data: Input dataclass, list, tuple, dict, or scalar.
        store: obstore store used for upload/download.
        mode: ``"persist"`` or ``"materialize"``.

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
            return await persist_file_reference(store, data)
        if mode == "materialize" and data.is_durable and data.local_path is None:
            return await materialize_file_reference(store, data)
        return data

    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        changes: dict[str, Any] = {}
        for f in dataclasses.fields(data):
            old_val = getattr(data, f.name)
            new_val = await _replace_refs(old_val, store, mode)
            if new_val is not old_val:
                changes[f.name] = new_val
        return dataclasses.replace(data, **changes) if changes else data

    if isinstance(data, list):
        new_list = [await _replace_refs(item, store, mode) for item in data]
        return (
            new_list
            if any(n is not o for n, o in zip(new_list, data))
            else data
        )

    if isinstance(data, tuple):
        new_tuple = tuple(await _replace_refs(item, store, mode) for item in data)
        return new_tuple if new_tuple != data else data

    return data


async def persist_file_refs(store: "ObjectStore", data: Any) -> Any:
    """Upload all ephemeral FileReferences in *data* to the store.

    Returns:
        New object tree with all ephemeral FileReferences replaced by
        durable ones.
    """
    return await _replace_refs(data, store, "persist")


async def materialize_file_refs(store: "ObjectStore", data: Any) -> Any:
    """Download all durable FileReferences in *data* to local temp files.

    Returns:
        New object tree with all durable FileReferences replaced by local
        ones (``local_path`` set).
    """
    return await _replace_refs(data, store, "materialize")
