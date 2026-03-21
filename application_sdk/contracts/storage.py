"""Contracts for the App.upload and App.download framework tasks."""

from __future__ import annotations

from dataclasses import dataclass, field

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, StorageTier


@dataclass
class UploadInput(Input):
    """Input for ``App.upload``.

    Describes a local file or directory to upload to the object store.
    ``local_path`` may point to a single file or a directory — the SDK
    detects which automatically.

    Args:
        local_path: Local file or directory path to upload.
        storage_path: Destination key (single file) or prefix (directory)
            in the store.  Auto-namespaced based on *tier* when ``None``.
        tier: Storage lifecycle tier.  Controls the destination prefix when
            *storage_path* is not given and sets ``tier`` on the returned
            ``FileReference`` so cleanup behaves correctly.
            Defaults to ``StorageTier.RETAINED`` (stored under the run-scoped
            ``artifacts/apps/`` prefix, not auto-cleaned).
        skip_if_exists: When ``True``, skip uploading files whose SHA-256
            hash already matches the stored value.  Defaults to ``False``.
    """

    local_path: str = ""
    storage_path: str | None = None
    tier: StorageTier = StorageTier.RETAINED
    skip_if_exists: bool = False


@dataclass
class UploadOutput(Output):
    """Output from ``App.upload``.

    Args:
        ref: Durable ``FileReference`` with both ``local_path`` and
            ``storage_path`` set.  ``file_count`` is 1 for a single file
            or the total number of files for a directory upload.
        synced: ``True`` if at least one file was actually transferred.
        reason: Human-readable transfer outcome
            (e.g. ``"uploaded"``, ``"skipped:hash_match"``).
    """

    ref: FileReference = field(default_factory=FileReference)
    synced: bool = False
    reason: str = ""


@dataclass
class DownloadInput(Input):
    """Input for ``App.download``.

    Describes what to download from the object store and where to put it.
    Exactly one of ``storage_path`` or ``ref`` must point to the store-side
    source; ``local_path`` is the destination.

    Args:
        storage_path: Store key (single file) or prefix (directory) to
            download.  Takes precedence over ``ref.storage_path`` when set.
        local_path: Local destination file path (single file) or directory
            (prefix download).  Uses a temp directory when ``None``.
        ref: Optional existing ``FileReference`` to rematerialise — its
            ``storage_path`` is used when ``storage_path`` is not provided
            directly.
        skip_if_exists: When ``True``, skip downloading files whose local
            SHA-256 hash already matches the stored value.
    """

    storage_path: str = ""
    local_path: str | None = None
    ref: FileReference | None = None
    skip_if_exists: bool = False


@dataclass
class DownloadOutput(Output):
    """Output from ``App.download``.

    Args:
        ref: Fully materialised ``FileReference`` with both ``local_path``
            and ``storage_path`` set.  ``file_count`` reflects the number
            of files downloaded (or skipped).
        synced: ``True`` if at least one file was actually transferred.
        reason: Human-readable transfer outcome.
    """

    ref: FileReference = field(default_factory=FileReference)
    synced: bool = False
    reason: str = ""
