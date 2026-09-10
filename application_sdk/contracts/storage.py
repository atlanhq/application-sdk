"""Contracts for the App.upload, App.download and App.verify_refs framework tasks."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import (
    FileReference,
    MaxItems,
    StorageTier,
    StoreTarget,
)


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


class VerifyRefsInput(Input):
    """Input for ``App.verify_refs``.

    Carries the producer's declaration of what a step wrote — the
    ``FileReference`` list its tasks actually returned — so the producer
    can assert the handoff is whole before naming a prefix downstream.

    A prefix scan cannot tell "absent" from "lost": listing
    ``transformed/`` and finding three of four entities looks exactly
    like a run that legitimately produced three.  Checking the declared
    refs distinguishes the two, which is what ``APP-CORRECTNESS-001``
    requires of every cross-activity handoff.

    Args:
        refs: The refs the producing tasks returned.  Each is looked up
            by its ``storage_path``; a ref with no ``storage_path``
            counts as missing, because a producer that could not say
            where it wrote has declared nothing.  Refs whose
            ``file_count`` exceeds 1 are treated as directory refs and
            verified by listing the prefix.
        prefix: Optional object-store prefix the refs are expected to
            live under — typically the prefix about to be handed
            downstream.  When set, a ref that resolves outside it fails
            verification: the prefix would not cover it, so a consumer
            walking the prefix would never see the data.
        store: Which store to check.  Defaults to
            :attr:`~application_sdk.contracts.types.StoreTarget.DEPLOYMENT`,
            the store the activity interceptor persists refs to.
    """

    refs: Annotated[list[FileReference], MaxItems(10000)] = Field(default_factory=list)
    prefix: str = ""
    store: StoreTarget = StoreTarget.DEPLOYMENT


class VerifyRefsOutput(Output):
    """Output from ``App.verify_refs``.

    Only ever returned on success — a hole raises
    :class:`~application_sdk.storage.errors.StorageHandoffIncompleteError`
    rather than reporting itself in a field a caller can forget to read.

    Args:
        verified_count: Number of refs confirmed present in the store.
        verified_file_count: Sum of ``file_count`` across the verified
            refs — the number of objects the declaration covers.
        prefix: The prefix every verified ref was confirmed to sit under,
            echoed back so the caller can log what it asserted.
    """

    verified_count: int = 0
    verified_file_count: int = 0
    prefix: str = ""


class DeclaredFile(BaseModel, frozen=True):
    """One entry in a producer's declaration of what it wrote.

    A bare ``FileReference`` says *where the bytes are*; it does not say what
    the file **is**.  When a fan-in delivery has to reshape keys — a per-entity
    tree under a new prefix — the identity is what decides the destination, and
    losing it is how a four-entity tree becomes four opaque blobs.

    Args:
        ref: The ``FileReference`` the producing task returned.
        label: What this file is (typically a typename or entity name), used as
            its leaf under the destination prefix.  Leave empty to derive the
            leaf from the ref's own key instead — see
            :meth:`~application_sdk.app.base.App.upload_refs`.
    """

    ref: FileReference
    label: str = ""

    model_config = ConfigDict(frozen=True)

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        cleaned = v.strip("/")
        if cleaned and (".." in PurePosixPath(cleaned).parts or "\x00" in v):
            raise ValueError(  # stdlib-interop: pydantic field_validator requires ValueError
                f"label must not contain path traversal segments: {v!r}"
            )
        return cleaned


class UploadRefsInput(Input):
    """Input for ``App.upload_refs``.

    Args:
        files: The producer's declaration — one entry per file the fanned-out
            step produced.  An empty list is meaningful: it says the step
            produced nothing, and ``upload_refs`` answers with an empty prefix
            rather than one naming an empty tree.
        prefix: Destination prefix every declared file lands under.  This is
            the prefix the caller then hands downstream.
        source_prefix: Prefix to strip from each ref's own key to get its leaf
            under *prefix*, for declarations whose keys already carry the shape
            the destination should keep (``<run>/transformed/<entity>/x.json``
            → ``<entity>/x.json``).  Ignored for any entry that carries a
            ``label``.  One of the two must yield a leaf for every entry — the
            SDK does not guess, because a guess that works for four entities
            and flattens one is worse than an error.
        tier: Storage lifecycle tier for the delivered copies.  Defaults to
            ``RETAINED`` — a handoff artifact must survive the producing run's
            cleanup.
        verify: When ``True`` (default), the delivery is checked back against
            the declaration before the task returns.  Turn it off only when a
            separate ``verify_refs`` call already covers the same objects.
    """

    files: Annotated[list[DeclaredFile], MaxItems(10000)] = Field(default_factory=list)
    prefix: str = ""
    source_prefix: str = ""
    tier: StorageTier = StorageTier.RETAINED
    verify: bool = True


class UploadRefsOutput(Output):
    """Output from ``App.upload_refs``.

    Args:
        prefix: The prefix the declaration was delivered to — **empty when the
            declaration was empty**.  Hand this downstream rather than the
            prefix you asked for: a consumer that diffs against an empty tree
            reads it as "delete everything", so an empty declaration must
            surface as an absent prefix, not a present-but-empty one.
        refs: Durable ``FileReference`` per delivered file, in declaration
            order.
        file_count: Total objects delivered across all refs.
    """

    prefix: str = ""
    refs: Annotated[list[FileReference], MaxItems(10000)] = Field(default_factory=list)
    file_count: int = 0
