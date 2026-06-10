"""Contracts for the App.upload and App.download framework tasks."""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import Field, field_validator

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, StorageTier


class UploadInput(Input):
    """Input for ``App.upload``.

    Describes a local file or directory to upload to the object store.
    ``local_path`` may point to a single file or a directory — the SDK
    detects which automatically.

    The SDK applies a three-step upload strategy internally:

    1. **Cross-store SHA-256 dedup** — if the deployment-store sidecar already
       matches the upstream sidecar the upload is skipped with no bytes
       transferred (idempotent retry support).
    2. **Local upload** — if ``local_path`` exists on this pod, the file is
       uploaded directly (no download cost).
    3. **Deployment-store fallback** — if ``local_path`` is absent (cross-pod
       SDR deployment or writer-deleted by ``use_consolidation=True``), the SDK
       streams from the deployment store instead.  All existing call sites gain
       this fallback for free with no API changes.

    Args:
        local_path: Local file or directory path to upload.
        ref: Optional existing ``FileReference`` to upload from — its
            ``storage_path`` is used as the deployment-store source key when
            ``local_path`` is absent (cross-pod or writer-deleted scenarios in
            SDR deployments).  Symmetric with ``DownloadInput.ref``.  Either
            provide ``ref`` directly or let the SDK derive it automatically from
            ``local_path`` — no call-site changes are required for existing
            connectors to gain the cross-pod fallback.
        storage_path: Destination key (single file) or prefix (directory)
            in the store.  Auto-namespaced based on *tier* when ``None``.
        storage_subdir: Subdirectory name appended to the auto-generated run prefix.
            Use this when uploading a directory whose name should be preserved
            in the object store (e.g. ``storage_subdir="dbt"`` places files at
            ``{run_prefix}/dbt/...``).  Ignored when *storage_path* is set.
        tier: Storage lifecycle tier.  Controls the destination prefix when
            *storage_path* is not given and sets ``tier`` on the returned
            ``FileReference`` so cleanup behaves correctly.
            Defaults to ``StorageTier.RETAINED`` (stored under the run-scoped
            ``artifacts/apps/`` prefix, not auto-cleaned).
        skip_if_exists: When ``True``, skip uploading files whose SHA-256
            hash already matches the stored value.  Defaults to ``False``.
        raise_on_empty: When ``True``, raise ``StorageEmptyUploadError`` if the upload
            completed with ``file_count == 0`` (i.e. ``local_path`` was an
            empty directory). Defaults to ``False`` to preserve historical
            silent-zero behavior that several incremental extractors rely
            on (a quiet-day run that finds no new data legitimately
            uploads zero files). Opt in (``True``) when zero files
            indicates a bug — e.g. a non-incremental extract that wrote
            nothing, or a directory the extract step was supposed to
            populate. See BLDX-1255 for the incident history (Tableau /
            Looker / Coalesce / dbt silent-failures) and the workaround
            patterns in dbt / databricks / coalesce connectors. Will flip
            to ``True`` default in v4.0 alongside ``BaseMetadataExtractor``
            removal.
    """

    local_path: str = ""
    ref: FileReference | None = None
    storage_path: str | None = None
    storage_subdir: str | None = None
    tier: StorageTier = StorageTier.RETAINED
    skip_if_exists: bool = False
    raise_on_empty: bool = False

    @field_validator("storage_subdir")
    @classmethod
    def _validate_storage_subdir(cls, v: str | None) -> str | None:
        if v is not None:
            cleaned = v.strip("/")
            if not cleaned or ".." in PurePosixPath(cleaned).parts or "\x00" in v:
                raise ValueError(  # stdlib-interop: pydantic field_validator requires ValueError
                    f"storage_subdir must not contain path traversal segments: {v!r}"
                )
        return v


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

    ref: FileReference = Field(default_factory=FileReference)
    synced: bool = False
    reason: str = ""


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


class DownloadOutput(Output):
    """Output from ``App.download``.

    Args:
        ref: Fully materialised ``FileReference`` with both ``local_path``
            and ``storage_path`` set.  ``file_count`` reflects the number
            of files downloaded (or skipped).
        synced: ``True`` if at least one file was actually transferred.
        reason: Human-readable transfer outcome.
    """

    ref: FileReference = Field(default_factory=FileReference)
    synced: bool = False
    reason: str = ""
