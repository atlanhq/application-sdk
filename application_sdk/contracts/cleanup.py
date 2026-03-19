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
