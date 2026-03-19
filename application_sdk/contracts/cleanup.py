"""Contracts for the App.cleanup_files framework task."""

from __future__ import annotations

from dataclasses import dataclass, field

from application_sdk.contracts.base import Input, Output


@dataclass
class CleanupInput(Input, allow_unbounded_fields=True):
    """Input for ``App.cleanup_files``.

    Args:
        extra_paths: Additional directory paths to delete beyond the defaults.
            When non-empty, these paths are used *instead of* the default
            convention-based temp directories (``CLEANUP_BASE_PATHS`` /
            ``TEMPORARY_PATH``).  Tracked ``FileReference`` local paths are
            always cleaned up regardless of this field.
    """

    extra_paths: list[str] = field(default_factory=list)


@dataclass
class CleanupOutput(Output, allow_unbounded_fields=True):
    """Output from ``App.cleanup_files``.

    Args:
        path_results: Per-path cleanup result (``True`` = deleted or
            already absent, ``False`` = error during deletion).
    """

    path_results: dict[str, bool] = field(default_factory=dict)


@dataclass
class StorageCleanupInput(Input, allow_unbounded_fields=True):
    """Input for ``App.cleanup_storage``.

    Args:
        include_prefix_cleanup: When ``True``, also delete objects under the
            run-scoped prefix ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}/``
            in addition to tracked ``file_refs/`` objects.  Off by default
            because run artifacts may be needed by downstream systems after
            completion.
    """

    include_prefix_cleanup: bool = False


@dataclass
class StorageCleanupOutput(Output, allow_unbounded_fields=True):
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
