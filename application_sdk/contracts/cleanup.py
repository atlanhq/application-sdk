"""Contracts for the App.cleanup_files framework task."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, MaxItems

__all__ = [
    "CleanupInput",
    "CleanupOutput",
    "STORAGE_CLEANUP_FILE_REFS_MAX",
    "StorageCleanupInput",
    "StorageCleanupOutput",
]


#: Most stored refs a workflow passes to ``cleanup_storage`` in
#: ``StorageCleanupInput.file_refs``. The list travels in the activity's input,
#: so it is bounded well under Temporal's payload limit (a ref serialises to a
#: few hundred bytes). Past it, the workflow keeps durable refs first, so the
#: durable-alias guard still sees them, and logs what it dropped.
STORAGE_CLEANUP_FILE_REFS_MAX = 2000


class CleanupInput(Input):
    """Input for ``App.cleanup_files``.

    Args:
        extra_paths: Additional directory paths to delete beyond the defaults.
            When non-empty, these paths are used *instead of* the default
            convention-based temp directories (``CLEANUP_BASE_PATHS`` /
            ``TEMPORARY_PATH``).  Tracked ``FileReference`` local paths are
            always cleaned up regardless of this field.
    """

    extra_paths: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)


class CleanupOutput(Output):
    """Output from ``App.cleanup_files``.

    Args:
        path_results: Per-path cleanup result (``True`` = deleted or
            already absent, ``False`` = error during deletion).
    """

    path_results: Annotated[dict[str, bool], MaxItems(10000)] = Field(
        default_factory=dict
    )


class StorageCleanupInput(Input):
    """Input for ``App.cleanup_storage``.

    Args:
        include_prefix_cleanup: When ``True``, also delete objects under the
            run-scoped prefix ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}/``
            in addition to tracked ``file_refs/`` objects.  Off by default
            because run artifacts may be needed by downstream systems after
            completion.
        file_refs: Stored ``FileReference`` objects the workflow read off
            every task's input and result during the run, whichever worker
            ran the task. ``cleanup_storage`` treats them as tracked, beside
            the refs the worker it runs on tracked itself, so a ``TRANSIENT``
            object another worker uploaded is deleted too, and a durable ref
            from another worker still guards its key. The default
            ``on_complete`` fills it in, with ``local_path`` unset and
            ``auto_materialize`` off, so the task never downloads them.
    """

    include_prefix_cleanup: bool = False
    file_refs: Annotated[
        list[FileReference], MaxItems(STORAGE_CLEANUP_FILE_REFS_MAX)
    ] = Field(default_factory=list)


class StorageCleanupOutput(Output):
    """Output from ``App.cleanup_storage``.

    Args:
        deleted_count: Number of object-store keys successfully deleted.
        skipped_count: Number of keys skipped (protected prefix or
            non-transient path).
        error_count: Number of keys that failed to delete.
    """

    deleted_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
