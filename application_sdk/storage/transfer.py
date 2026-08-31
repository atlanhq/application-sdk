"""Lower-level upload / download implementation used by the App tasks.

These functions can also be called directly from within an existing ``@task``
when task-wrapping is not desired (edge-case opt-in).

SHA-256-based deduplication
---------------------------
Every uploaded object gets a tiny sidecar ``{key}.sha256`` stored alongside it
in the object store — written by ``ops.upload_file`` itself, so it exists for
every upload path in the SDK and not just this one (FND-306).  On subsequent
uploads (``skip_if_exists=True``) the local file hash is compared against the
sidecar; the upload is skipped when they match.  The same sidecar is what the
transfer primitives verify downloads against, so a truncated artifact is caught
here rather than in a downstream parser; see ``storage.integrity``.

Cross-store deduplication (SDR deployments)
-------------------------------------------
``upload()`` accepts optional ``source_ref`` and ``source_store`` parameters
(the deployment store and its file reference).  When both are supplied the
function applies a three-step strategy per file:

1. **Cross-store SHA-256 dedup** — compare the deployment-store sidecar
   against the upstream-store sidecar; skip if they match (idempotent retry,
   no bytes transferred).
2. **Local upload** — if ``local_path`` exists on this pod, upload directly.
3. **Deployment-store fallback** — if ``local_path`` is absent (cross-pod or
   writer-deleted), stream from the deployment store to the upstream store.

``App.upload()`` always passes ``source_store=self.context.storage`` so all
callers gain the fallback automatically on the next SDK bump.

This approach is backend-agnostic (no reliance on ETag formats) and works
identically for single files and directories.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.common._listing import safe_list_directory
from application_sdk.constants import MAX_CONCURRENT_STORAGE_TRANSFERS
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.observability.logger_adaptor import get_logger

# The batch key listers are imported at top level, unlike the other
# storage-sibling imports in this module which are lazy: ``batch`` depends only
# on ``storage.ops`` / ``storage.integrity`` and never on ``transfer``, so
# ``transfer → batch`` is unconditionally acyclic — the import is safe
# regardless of module load order.
from application_sdk.storage.batch import list_data_keys, list_data_objects, list_keys

# Sidecar naming, digest computation and the read/write of ``{key}.sha256`` all
# live in ``storage.integrity``, which ``ops`` calls on every transfer. This
# module holds no copy of them: a second implementation of the digest protocol
# is how the reader and the writer drift apart.
from application_sdk.storage.integrity import read_expected_digest, sha256_file

_logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from obstore.store import ObjectStore

    from application_sdk.contracts.storage import DownloadOutput, UploadOutput


async def _upload_one(
    store: ObjectStore,
    local_file: Path,
    store_key: str,
    *,
    skip_if_exists: bool,
) -> tuple[bool, str]:
    """Upload a single file.  Returns ``(transferred, reason)``.

    The ``{key}.sha256`` sidecar is written by ``upload_file`` itself, after it
    has validated that what landed in the store is what was sent — so it is not
    written here (FND-306).
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        upload_file,
    )

    if skip_if_exists:
        local_digest = await sha256_file(local_file)
        remote_digest = await read_expected_digest(store, store_key)
        if remote_digest == local_digest:
            # A skip is still one file resolved. Directory uploads that skip
            # thousands of hash-matching files on an idempotent retry are doing
            # real work (a digest and a sidecar GET each) and must not read as
            # a stall.
            current_progress_tracker().mark_progress("storage.upload_file")
            return False, "skipped:hash_match"

    await upload_file(store_key, local_file, store, normalize=False)
    # Per-file boundary, on top of the per-part marks inside upload_file: this
    # is the label an operator wants in a stall message, since it says the
    # attempt was moving whole files rather than stuck mid-transfer on one.
    # (The sidecar write that used to sit here is upload_file's job now — see
    # the docstring above.)
    current_progress_tracker().mark_progress("storage.upload_file")
    return True, "uploaded"


async def _cross_store_sha256_match(
    source_store: ObjectStore,
    source_key: str,
    target_store: ObjectStore,
    target_key: str,
) -> bool:
    """Return ``True`` if both stores have identical non-``None`` SHA-256 sidecars.

    Used as step 1 of the three-step upload strategy to avoid transferring bytes
    that are already current in the target store (idempotent retry support).
    Returns ``False`` when either sidecar is absent so the upload proceeds.
    """
    source_digest = await read_expected_digest(source_store, source_key)
    if source_digest is None:
        return False
    target_digest = await read_expected_digest(target_store, target_key)
    return source_digest == target_digest


async def _upload_from_store(
    source_store: ObjectStore,
    source_key: str,
    target_store: ObjectStore,
    target_key: str,
    *,
    source_listed: bool = False,
) -> tuple[bool, str]:
    """Upload a single file from *source_store* to *target_store*.

    Implements steps 1 and 3 of the three-step upload strategy:

    * Step 0 — same-object guard: when *source_store* and *target_store* are the
      same store and the keys are identical, the object already is its own
      destination — **provided it is there**.  Presence is taken from
      *source_listed*, or established with one HEAD when the key came from a
      caller-supplied ``FileReference``; only then does this return immediately,
      without even the two sidecar GETs the SHA-256 dedup would cost.  An absent
      object falls through to the copy path below and raises
      ``StorageNotFoundError`` like any other leg would.  This is the
      key-preserving deployment→deployment leg of the ADR-0014 dual write
      (FND-536).
    * Step 1 — cross-store SHA-256 dedup: skips the transfer when both stores
      already hold the same content at their respective keys.
    * Step 3 — deployment-store fallback: downloads to a temporary local file
      and re-uploads to the target.  Both legs validate: the download against
      the source's sidecar, the upload against what the target reports back,
      and ``upload_file`` writes the target's own sidecar (FND-306).

    Args:
        source_store: Store to read *source_key* from.
        source_key: Key to copy.
        target_store: Store to write *target_key* to.
        target_key: Destination key.
        source_listed: ``True`` when the caller obtained *source_key* by listing
            *source_store*, which proves the object is there.  Lets the
            same-object guard skip its existence HEAD; leave ``False`` whenever
            the key came from a caller-supplied ``FileReference``.

    Returns ``(transferred, reason)``.
    """
    from application_sdk.storage.chunked import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _part_path,
        _transfer_state_path,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        download_file_chunked,
        exists,
        upload_file,
    )

    if source_store is target_store and source_key == target_key:
        # Copying an object onto itself: no bytes to move and no sidecar to
        # compare. But "satisfied" must mean the object is actually there — a
        # stale FileReference pinned to a key that was never written would
        # otherwise buy a durable-looking success out of nothing. When the key
        # came from a listing that is already proven; otherwise one HEAD settles
        # it, and an absent object falls through to the copy path below, which
        # fails with the same not-found error any other leg would raise.
        if source_listed or await exists(source_key, source_store, normalize=False):
            # Still progress for the heartbeat — one key resolved.
            current_progress_tracker().mark_progress("storage.copy_file")
            return False, "skipped:same_object"

    if await _cross_store_sha256_match(
        source_store, source_key, target_store, target_key
    ):
        # Two sidecar GETs per key: a fallback reconcile over a large prefix
        # that skips everything is still forward progress.
        current_progress_tracker().mark_progress("storage.copy_file")
        return False, "skipped:hash_match"

    fd, tmp_path_str = tempfile.mkstemp()
    os.close(fd)
    tmp = Path(tmp_path_str)
    try:
        # Chunk large source objects (bounded range GETs) so a big deployment-store
        # file survives slow egress on the cross-store copy path. (BLDX-1513)
        # resume=False: mkstemp yields a fresh name per call, so a checkpoint
        # sidecar could never be reused — without this, a failed copy strands
        # a checkpoint under /tmp until pod restart.
        await download_file_chunked(
            source_key, tmp, source_store, normalize=False, resume=False
        )
        await upload_file(target_key, tmp, target_store, normalize=False)
        current_progress_tracker().mark_progress("storage.copy_file")
        return True, "uploaded"
    finally:
        tmp.unlink(missing_ok=True)
        # Belt-and-braces: never strand either staging file for a temp
        # destination. Both live in `.sdk-partial/` beside it, so ask the
        # helpers rather than rebuilding the names here — a local copy of the
        # layout is how a cleanup site silently stops matching the writer
        # (CONNECT-1126). The part file is the one that can actually survive
        # `resume=False`: a publish failure leaves it on disk deliberately, and
        # with a fresh mkstemp name no later attempt will ever claim it.
        _part_path(tmp).unlink(missing_ok=True)
        _transfer_state_path(tmp).unlink(missing_ok=True)


async def _download_one(
    store: ObjectStore,
    store_key: str,
    local_file: Path,
    *,
    skip_if_exists: bool,
    file_size: int | None = None,
    etag: str | None = None,
    resume: bool | None = None,
    sidecar_present: bool | None = None,
) -> tuple[bool, str]:
    """Download a single file.  Returns ``(transferred, reason)``.

    *file_size* / *etag* (when known from a prior listing) are threaded to the
    chunked path so large objects fetch via bounded, version-pinned range GETs
    without a per-file HEAD; ``None`` lets the chunked path HEAD once
    (single-file case). Pass ``resume=False`` when *local_file* is a
    fresh-named temp file — its checkpoint sidecar could never be reused, so
    it would only be stranded on failure. (BLDX-1513 / BLDX-1523)

    *sidecar_present* is threaded the same way: a prefix download's listing
    already knows whether ``{key}.sha256`` exists, so the integrity check below
    the call does not have to probe for it per file (FND-306).
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        download_file_chunked,
    )

    remote_digest: str | None = None
    if skip_if_exists and local_file.exists():
        remote_digest = await read_expected_digest(
            store, store_key, sidecar_present=sidecar_present
        )
        if remote_digest is not None:
            local_digest = await sha256_file(local_file)
            if local_digest == remote_digest:
                # See _upload_one: a hash-match skip is one file resolved, and
                # the prefix download loop below runs these sequentially.
                current_progress_tracker().mark_progress("storage.download_file")
                return False, "skipped:hash_match"

    local_file.parent.mkdir(parents=True, exist_ok=True)
    await download_file_chunked(
        store_key,
        local_file,
        store,
        normalize=False,
        file_size=file_size,
        etag=etag,
        resume=resume,
        # Already fetched above when skip_if_exists forced a comparison —
        # hand it down so the transfer is verified without re-reading it.
        expected_sha256=remote_digest,
        sidecar_present=sidecar_present,
    )
    current_progress_tracker().mark_progress("storage.download_file")
    return True, "downloaded"


# System directories that must never be uploaded.
_SENSITIVE_SYSTEM_PREFIXES = (
    "/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/root/",
    "/private/etc/",
)

# Hidden credential/config directories that must never be uploaded.
_SENSITIVE_DIR_NAMES = frozenset({".aws", ".ssh", ".gnupg", ".kube", ".vault"})

# File name prefixes for environment/secret files.
_SENSITIVE_FILE_PREFIXES = (".env",)


def _parse_blocked_paths() -> list[str]:
    """Parse ATLAN_UPLOAD_FILE_BLOCKED_PATHS env var (comma-separated patterns)."""
    val = os.environ.get("ATLAN_UPLOAD_FILE_BLOCKED_PATHS", "")
    return [p.strip() for p in val.split(",") if p.strip()] if val else []


def _validate_upload_path(path: Path) -> None:
    """Block uploads from sensitive system paths, credential dirs, and env files."""
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        UnsafeUploadPathError,
    )

    if ".." in path.parts:
        raise UnsafeUploadPathError(unsafe_path=str(path))

    resolved = path.resolve()
    resolved_str = str(resolved)

    if resolved_str.startswith(_SENSITIVE_SYSTEM_PREFIXES):
        raise UnsafeUploadPathError(unsafe_path=str(path))

    if any(part in _SENSITIVE_DIR_NAMES for part in resolved.parts):
        raise UnsafeUploadPathError(unsafe_path=str(path))

    if resolved.is_file() and resolved.name.startswith(_SENSITIVE_FILE_PREFIXES):
        raise UnsafeUploadPathError(unsafe_path=str(path))

    # User-defined blocked paths via ATLAN_UPLOAD_FILE_BLOCKED_PATHS (comma-separated).
    # Each entry is matched as a substring against the full resolved path.
    # e.g. ATLAN_UPLOAD_FILE_BLOCKED_PATHS="/custom/secrets/,.vault,.credentials"
    user_blocked = _parse_blocked_paths()
    if any(pattern in resolved_str for pattern in user_blocked):
        raise UnsafeUploadPathError(unsafe_path=str(path))


def _derive_target_key(
    storage_path: str | None,
    _app_prefix: str,
    storage_subdir: str | None,
    leaf_name: str,
    normalize_key_fn: Callable[[str], str],
    *,
    append_leaf: bool = True,
) -> str:
    """Compute the target object-store key or prefix from call-site context.

    All four upload branches share the same key-derivation logic — this
    helper is the single source of truth so future changes (e.g. a new
    tier or namespace segment) only require one edit.

    Args:
        storage_path: Explicit destination key/prefix — returned as-is when set.
        _app_prefix: Run-scoped prefix injected by ``App.upload()``.
        storage_subdir: Optional subdirectory segment appended after *_app_prefix*.
        leaf_name: Filename for single-file uploads; directory basename when
            there is no *_app_prefix* (the ``else`` fallback).  Pass ``""``
            for directory-prefix derivation when *_app_prefix* is known to be
            set (``append_leaf=False`` then makes the leaf a no-op).
        normalize_key_fn: ``normalize_key`` from ``storage.ops`` (injected to
            avoid a repeated lazy import at every call site).
        append_leaf: When ``True`` (file mode), *leaf_name* is appended after
            the base.  When ``False`` (dir-prefix mode), *leaf_name* is only
            used as the fallback when neither *storage_path* nor *_app_prefix*
            is available.
    """
    if storage_path is not None:
        return normalize_key_fn(storage_path)
    if _app_prefix:
        base = (
            f"{_app_prefix}/{normalize_key_fn(storage_subdir)}"
            if storage_subdir
            else _app_prefix
        )
        return f"{base}/{leaf_name}" if (append_leaf and leaf_name) else base
    # No explicit prefix: fall back to leaf_name only (local branches) or ""
    # (fallback branches — callers must guard against the empty result).
    return leaf_name


def _make_upload_output(
    local_path: str | None,
    storage_key: str,
    file_count: int,
    tier: StorageTier,
    transferred_count: int,
    reason: str,
    *,
    is_dir: bool = False,
) -> UploadOutput:
    """Build an ``UploadOutput`` from the common fields shared across all branches.

    All four upload branches construct identical ``FileReference`` + ``UploadOutput``
    objects — this helper eliminates the duplication and ensures ``is_durable=True``
    is always set.

    Args:
        local_path: Local path stored on the ``FileReference`` (may be ``None``
            for fallback refs whose file was never materialised on this pod).
        storage_key: Object-store key (file) or prefix (directory).  For
            directories, a trailing ``/`` is appended automatically when
            *is_dir* is ``True``.
        file_count: Number of files covered by the reference.
        tier: Storage lifecycle tier forwarded to ``FileReference``.
        transferred_count: Number of files actually transferred (0 = all
            skipped via SHA-256 match).
        reason: Human-readable transfer outcome (e.g. ``"uploaded"``).
        is_dir: When ``True``, ensures ``storage_key`` ends with ``"/"`` as
            the canonical object-store prefix convention.
    """
    from application_sdk.contracts.storage import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        UploadOutput,
    )

    store_path = (storage_key.rstrip("/") + "/") if is_dir else storage_key
    ref = FileReference(
        local_path=local_path,
        storage_path=store_path,
        is_durable=True,
        file_count=file_count,
        tier=tier,
    )
    return UploadOutput(ref=ref, synced=transferred_count > 0, reason=reason)


_FALLBACK_PREFIX_REQUIRED = (
    "upload fallback: _app_prefix or storage_path is required when using the "
    "deployment-store fallback. App.upload() always sets _app_prefix; if calling "
    "transfer.upload() directly, supply _app_prefix or storage_path."
)


async def _list_source_data_keys(
    source_storage_path: str, source_store: ObjectStore
) -> tuple[str, list[str]]:
    """List non-sidecar data keys under an SDR source-store directory.

    Shared by the partial-local reconcile branch and the local-absent
    deployment-store fallback in :func:`upload`, so the ``..`` path-traversal
    guard and the source-listing sequence stay identical between them (a future
    change to path validation can't drift between the two).

    Returns the normalised ``source_dir_prefix`` (trailing slash) and the data
    keys beneath it, sidecar (``.sha256``) keys excluded.

    Raises:
        UnsafeUploadPathError: If the normalised source path contains ``..``.
    """
    from pathlib import PurePosixPath  # noqa: PLC0415 — stdlib; lazy use only

    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        normalize_key,
    )

    source_norm = normalize_key(source_storage_path)
    if source_norm and ".." in PurePosixPath(source_norm).parts:
        from application_sdk.storage.errors import (  # noqa: PLC0415
            UnsafeUploadPathError,
        )

        raise UnsafeUploadPathError(unsafe_path=source_storage_path)
    source_dir_prefix = source_norm.rstrip("/") + "/"
    keys = await list_data_keys(source_dir_prefix, source_store, normalize=False)
    return source_dir_prefix, keys


async def upload(
    local_path: str,
    storage_path: str | None = None,
    *,
    storage_subdir: str | None = None,
    skip_if_exists: bool = False,
    raise_on_empty: bool = False,
    store: ObjectStore | None = None,
    _source_ref: FileReference | None = None,
    _source_store: ObjectStore | None = None,
    _app_prefix: str = "",
    _tier: StorageTier = StorageTier.RETAINED,
    max_concurrency: int = MAX_CONCURRENT_STORAGE_TRANSFERS,
) -> UploadOutput:
    """Upload a local file or directory to the object store.

    When *storage_path* is ``None`` and *_app_prefix* is provided the key /
    prefix is auto-namespaced as ``{_app_prefix}/{filename}`` (files) or
    ``{_app_prefix}/`` (directories).

    When *storage_subdir* is set and *storage_path* is ``None``, the subdir name is
    appended to _app_prefix so files land at ``{_app_prefix}/{storage_subdir}/...``.
    This preserves the directory name in the object store path.

    Two-step upload strategy (when *_source_ref* and *_source_store* are supplied):

    1. **Local upload** — if *local_path* exists on this pod, upload directly.
       ``_upload_one`` handles per-file SHA-256 dedup when *skip_if_exists* is set.
    2. **Deployment-store fallback** — if *local_path* is absent (cross-pod
       KEDA-scaled SDR worker or writer-deleted by ``use_consolidation=True``),
       stream from *_source_store* to the target.  ``_upload_from_store`` performs
       a cross-store SHA-256 sidecar check before transferring bytes so a second
       call for the same file short-circuits (idempotent replay support).
    3. **Partial-local reconcile** — if *local_path* is a directory that exists
       but holds only a subset of the tree (e.g. transform activities scheduled
       across pods), upload the local files AND stream any file present in
       *_source_store* but missing locally, so the target copy is complete
       regardless of pod placement.  This fires one ``list_keys`` LIST against
       the source store per SDR directory upload — including when the local
       copy is already complete — since partial-ness cannot be detected without
       listing.  The LIST is bounded and off the per-file transfer path.

    ``App.upload()`` automatically derives *_source_ref* from *local_path* and
    always passes ``_source_store=self.context.storage``, so all existing call
    sites gain the fallback for free without API changes.

    **Step-2 prerequisite:** the file must have been produced by a ``@task`` that
    returned a ``FileReference`` — the SDK interceptor auto-uploads every returned
    ``FileReference`` to the deployment store on task completion.  Files written
    directly to the local filesystem inside a ``@task`` *without* flowing through
    a ``FileReference`` return are NOT replicated to the deployment store; step 2
    will fail with ``StorageError`` in that case.

    Args:
        local_path: Local file or directory to upload.
        storage_path: Destination key or prefix override.  Takes priority
            over *storage_subdir* and *_app_prefix* when set.
        storage_subdir: Subdirectory name appended to the auto-generated run prefix.
            Ignored when *storage_path* is set.
        skip_if_exists: Skip files whose local SHA-256 matches the stored sidecar.
        raise_on_empty: When ``True``, raise ``StorageEmptyUploadError`` if
            *local_path* is a directory that contains zero files. Opt-in
            fail-loud for connectors where empty output indicates a bug
            (see BLDX-1255). Applies to the local-directory branch only.
        store: Target object store (upstream in SDR, deployment otherwise), or
            ``None`` to resolve from infrastructure.
        _source_ref: Internal — pre-computed ``FileReference`` carrying both
            ``local_path`` and ``storage_path`` for the deployment store.
            Derived and supplied exclusively by ``App.upload()``.
        _source_store: Internal — deployment-store binding used as the step-2
            fallback source.  Always ``self.context.storage`` when called via
            ``App.upload()``.
        _app_prefix: Internal prefix injected by the ``App.upload`` task.
        max_concurrency: Maximum parallel uploads for directory mode
            (default :data:`~application_sdk.constants.MAX_CONCURRENT_STORAGE_TRANSFERS`).

    Returns:
        :class:`~application_sdk.contracts.storage.UploadOutput`

    Raises:
        StorageError: If *local_path* does not exist or is neither a file
            nor a directory, and no *_source_ref* / *_source_store* fallback
            is available.
        StorageEmptyUploadError: When *raise_on_empty* is ``True`` and
            *local_path* is a directory containing zero files
            (category=DATA_INTEGRITY, audience=APP_OWNER, retryable=False).
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageError,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _resolve_store,
        normalize_key,
    )

    resolved = _resolve_store(store)
    source_resolved = (
        _resolve_store(_source_store) if _source_store is not None else None
    )
    src = Path(local_path)

    # Block sensitive local paths (runs even when the path does not exist locally
    # so uploads from sensitive locations are refused regardless of fallback path).
    # Guard: only validate when local_path is non-empty — Path("") resolves to
    # CWD on Python/macOS, so is_dir() returns True and would upload the tree.
    #
    # Note: ATLAN_UPLOAD_FILE_BLOCKED_PATHS substring matching applies to
    # local_path only.  When the caller passes only a ref (local_path="") the
    # source is already an object-store key in the deployment store, not a local
    # filesystem path, so the env-var blocklist has no meaningful scope there.
    # The ".." path-traversal check on _source_ref.storage_path (below) covers
    # the key-injection threat for that code path.
    if local_path:
        _validate_upload_path(src)

    if storage_subdir:
        from pathlib import PurePosixPath  # noqa: PLC0415 — stdlib; lazy use only

        cleaned = storage_subdir.strip("/")
        if (
            not cleaned
            or ".." in PurePosixPath(cleaned).parts
            or "\x00" in storage_subdir
        ):
            from application_sdk.storage.errors import (  # noqa: PLC0415
                UnsafeUploadPathError,
            )

            raise UnsafeUploadPathError(unsafe_path=storage_subdir)
        storage_subdir = cleaned

    if local_path and src.is_file():
        # ── Single file (local exists) ────────────────────────────────────
        key = _derive_target_key(
            storage_path, _app_prefix, storage_subdir, src.name, normalize_key
        )
        transferred, reason = await _upload_one(
            resolved, src, key, skip_if_exists=skip_if_exists
        )
        return _make_upload_output(str(src), key, 1, _tier, int(transferred), reason)

    elif local_path and src.is_dir():
        # ── Directory (local exists) ──────────────────────────────────────
        # A locally-present directory may be *incomplete* (BLDX-1554): when the
        # parallel transform activities that populated this tree were scheduled
        # across multiple worker pods, only the subset produced on *this* pod is
        # on local disk. Uploading just the local files would silently drop whole
        # entity types from the target — the failure mode that produced orphaned
        # child entities downstream.
        #
        # To keep the target copy complete regardless of pod placement, reconcile
        # against the source (deployment) store: upload every local file AND
        # stream any file that exists in the source store but is missing locally.
        # This extends the local-absent fallback (below) — which the docstring
        # already promises for "cross-pod KEDA-scaled SDR workers" — to the
        # partially-present case. Reconciliation runs only when a *distinct*
        # source store is supplied (i.e. an SDR hand-off where upstream !=
        # deployment); in non-SDR uploads ``_source_store`` is ``None`` / the
        # same store, so local disk stays authoritative and behaviour is
        # unchanged.
        prefix = _derive_target_key(
            storage_path,
            _app_prefix,
            storage_subdir,
            src.name,
            normalize_key,
            append_leaf=False,
        )
        # run_in_thread keeps the blocking fsync + scandir off the event loop,
        # using the dedicated pool rather than asyncio's default executor.
        files = await run_in_thread(safe_list_directory, src)
        local_rels = {str(fp.relative_to(src)).replace(os.sep, "/") for fp in files}

        # (source_key, target_key) pairs for files present in the source store
        # but absent from this pod's local copy.
        reconcile_pairs: list[tuple[str, str]] = []
        if (
            source_resolved is not None
            and source_resolved is not resolved
            and _source_ref is not None
            and _source_ref.storage_path
        ):
            source_dir_prefix, source_keys = await _list_source_data_keys(
                _source_ref.storage_path, source_resolved
            )
            for source_key in source_keys:
                rel = source_key.removeprefix(source_dir_prefix)
                if rel in local_rels:
                    continue  # present locally — uploaded from the fast path below
                target_key = f"{prefix}/{rel}" if prefix else rel
                reconcile_pairs.append((source_key, target_key))

        if raise_on_empty and not files and not reconcile_pairs:
            from application_sdk.storage.errors import (  # noqa: PLC0415
                StorageEmptyUploadError,
            )

            raise StorageEmptyUploadError(
                f"upload(local_path={local_path!r}): directory contains "
                "zero files. Either the extract step produced no output, "
                "or files were written to a different path than the one "
                "passed here. If quiet-day empty uploads are expected "
                "(e.g. incremental extracts with no new data), drop "
                "``raise_on_empty=True``. Otherwise verify the extract "
                "wrote files to the expected ``local_path``. See the "
                "dbt / databricks / coalesce connectors for the "
                "stream-uploaded-per-file workaround pattern.",
                local_path=local_path,
            )
        sem = asyncio.Semaphore(max_concurrency)
        keys = [
            f"{prefix}/{str(fp.relative_to(src)).replace(os.sep, '/')}"
            if prefix
            else str(fp.relative_to(src)).replace(os.sep, "/")
            for fp in files
        ]

        async def _bounded_upload(file_path: Path, fkey: str) -> bool:
            async with sem:
                ok, _ = await _upload_one(
                    resolved, file_path, fkey, skip_if_exists=skip_if_exists
                )
                return ok

        async def _bounded_reconcile(source_key: str, target_key: str) -> bool:
            # source_resolved is non-None whenever reconcile_pairs is populated
            # (guarded where reconcile_pairs is built). Explicit check rather than
            # ``assert`` so it is not stripped under ``python -O`` and narrows the
            # type for the ``_upload_from_store`` call below.
            if source_resolved is None:  # pragma: no cover — structurally unreachable
                raise RuntimeError(
                    "reconcile requires a resolved source store but none was set"
                )
            async with sem:
                ok, _ = await _upload_from_store(
                    source_resolved,
                    source_key,
                    resolved,
                    target_key,
                    source_listed=True,  # came from _list_source_data_keys
                )
                return ok

        # conformance: ignore[E010] results checked immediately below: errs filters BaseException, first is re-raised and rest are logged
        results = await asyncio.gather(
            *[_bounded_upload(fp, k) for fp, k in zip(files, keys)],
            *[_bounded_reconcile(sk, tk) for sk, tk in reconcile_pairs],
            return_exceptions=True,
        )
        errs = [r for r in results if isinstance(r, BaseException)]
        if errs:
            for extra in errs[1:]:
                _logger.error("concurrent upload failure (suppressed)", exc_info=extra)
            raise errs[0]
        n = sum(1 for ok in results if ok)
        total_files = len(files) + len(reconcile_pairs)
        reason = "uploaded" if n > 0 else "skipped:hash_match"
        if reconcile_pairs:
            _logger.info(
                "upload dir reconciled against source store: %d local + %d "
                "streamed from source (prefix=%s)",
                len(files),
                len(reconcile_pairs),
                prefix,
            )
        return _make_upload_output(
            str(src), prefix, total_files, _tier, n, reason, is_dir=True
        )

    elif (
        _source_ref is not None
        and _source_ref.storage_path
        and source_resolved is not None
    ):
        # ── Deployment-store fallback (local path absent) ──────────────────
        source_dir_prefix, data_dir_keys = await _list_source_data_keys(
            _source_ref.storage_path, source_resolved
        )
        # Normalised source key for the single-file fallback below;
        # _list_source_data_keys returns the directory prefix (trailing slash).
        source_norm = source_dir_prefix.rstrip("/")

        if data_dir_keys:
            # ── Directory fallback ─────────────────────────────────────────
            fallback_prefix = _derive_target_key(
                storage_path,
                _app_prefix,
                storage_subdir,
                "",
                normalize_key,
                append_leaf=False,
            )
            if not fallback_prefix:
                raise StorageError(_FALLBACK_PREFIX_REQUIRED)
            sem = asyncio.Semaphore(max_concurrency)

            async def _bounded_fallback(source_key: str) -> bool:
                async with sem:
                    rel = source_key.removeprefix(source_dir_prefix)
                    ok, _ = await _upload_from_store(
                        source_resolved,
                        source_key,
                        resolved,
                        f"{fallback_prefix}/{rel}" if fallback_prefix else rel,
                        source_listed=True,  # came from _list_source_data_keys
                    )
                    return ok

            results = await asyncio.gather(
                *[_bounded_fallback(k) for k in data_dir_keys], return_exceptions=True
            )
            errs = [r for r in results if isinstance(r, BaseException)]
            if errs:
                for extra in errs[1:]:
                    _logger.error(
                        "concurrent upload failure (suppressed)", exc_info=extra
                    )
                raise errs[0]
            n = sum(1 for ok in results if ok)
            reason = "uploaded" if n > 0 else "skipped:hash_match"
            return _make_upload_output(
                _source_ref.local_path,
                fallback_prefix,
                len(data_dir_keys),
                _tier,
                n,
                reason,
                is_dir=True,
            )

        else:
            # ── Single file fallback ───────────────────────────────────────
            leaf = (
                Path(_source_ref.local_path).name
                if _source_ref.local_path
                else source_norm.rsplit("/", 1)[-1]
            )
            fallback_key = _derive_target_key(
                storage_path, _app_prefix, storage_subdir, leaf, normalize_key
            )
            if not fallback_key:
                raise StorageError(_FALLBACK_PREFIX_REQUIRED)
            transferred, reason = await _upload_from_store(
                source_resolved, source_norm, resolved, fallback_key
            )
            return _make_upload_output(
                _source_ref.local_path, fallback_key, 1, _tier, int(transferred), reason
            )

    else:
        raise StorageError(
            f"local_path does not exist or is not a file/directory: {local_path}"
        )


async def download(
    storage_path: str,
    local_path: str | None = None,
    *,
    skip_if_exists: bool = False,
    store: ObjectStore | None = None,
) -> DownloadOutput:
    """Download a key or prefix from the object store to a local path.

    When *storage_path* ends with ``/`` (or matches multiple keys) the
    download is treated as a prefix/directory operation.  Otherwise it is
    treated as a single-file download.

    Args:
        storage_path: Store key (single file) or prefix (directory) to fetch.
        local_path: Local destination.  Defaults to a temp file/directory.
        skip_if_exists: Skip files whose local SHA-256 matches the stored sidecar.
        store: Object store to use, or ``None`` to resolve from infrastructure.

    Returns:
        :class:`~application_sdk.contracts.storage.DownloadOutput`
    """
    from application_sdk.contracts.storage import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        DownloadOutput,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _resolve_store,
        _safe_join_under,
        normalize_key,
    )

    resolved = _resolve_store(store)
    norm_path = normalize_key(storage_path)

    # Determine if this is a single-key or prefix download.
    # Heuristic: if the caller passed a trailing "/", or if listing with the
    # exact key returns 0 results but listing as prefix returns >0, treat as
    # directory.  We check the trailing slash first (explicit intent).
    is_prefix = storage_path.endswith("/") or norm_path.endswith("/")

    if not is_prefix:
        # Try listing as exact key to confirm it's a single object.
        single_keys = await list_keys(norm_path + "/", resolved, normalize=False)
        # If there are keys under this as a prefix it's actually a directory.
        if single_keys:
            is_prefix = True

    if not is_prefix:
        # ── Single file ────────────────────────────────────────────────────
        owns_temp = False
        if local_path is not None:
            dest = Path(local_path)
        else:
            suffix = Path(norm_path).suffix or ""
            fd, tmp = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            dest = Path(tmp)
            owns_temp = True

        try:
            # resume=False for owned temps: mkstemp yields a fresh name per
            # call, so a checkpoint sidecar could never be reused — it would
            # only be stranded under /tmp on failure. Caller-supplied stable
            # destinations keep the default (env-driven) resume behaviour.
            transferred, reason = await _download_one(
                resolved,
                norm_path,
                dest,
                skip_if_exists=skip_if_exists,
                resume=False if owns_temp else None,
            )
        # conformance: ignore[E004] cleanup-only handler that always re-raises; no logging needed here
        except BaseException:
            # Don't leave an empty temp file behind on download failure
            # (BLDX-1155 #5).
            if owns_temp:
                try:
                    from application_sdk.storage.chunked import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                        _part_path,
                        _transfer_state_path,
                    )

                    dest.unlink(missing_ok=True)
                    # Belt-and-braces: never strand either staging file for a
                    # temp destination either — see the matching cleanup in
                    # _upload_from_store for why these come from the helpers.
                    _part_path(dest).unlink(missing_ok=True)
                    _transfer_state_path(dest).unlink(missing_ok=True)
                except OSError:  # conformance: ignore[E002] best-effort cleanup of partial download; original error re-raised below
                    pass
            raise
        ref = FileReference(
            local_path=str(dest),
            storage_path=norm_path,
            is_durable=True,
            file_count=1,
        )
        return DownloadOutput(ref=ref, synced=transferred, reason=reason)

    else:
        # ── Directory / prefix ─────────────────────────────────────────────
        prefix = norm_path.rstrip("/") + "/"
        # Listing carries per-object sizes, so large files chunk without a
        # per-file HEAD (BLDX-1513); it also records which objects have a
        # SHA-256 sidecar, so integrity verification costs no extra probe
        # (FND-306). Sidecars themselves are excluded by the helper.
        data_objects = await list_data_objects(prefix, resolved, normalize=False)

        if local_path is not None:
            dest_dir = Path(local_path)
        else:
            dest_dir = Path(tempfile.mkdtemp())

        dest_dir.mkdir(parents=True, exist_ok=True)
        strip = prefix

        transferred_count = 0
        for obj in data_objects:
            rel = obj.key.removeprefix(strip)
            # Reject keys whose resolved path escapes dest_dir (e.g. via ".." segments).
            local_file = _safe_join_under(dest_dir, rel)
            ok, _ = await _download_one(
                resolved,
                obj.key,
                local_file,
                skip_if_exists=skip_if_exists,
                file_size=obj.size,
                etag=obj.etag,
                sidecar_present=obj.has_sidecar,
            )
            if ok:
                transferred_count += 1

        reason = "downloaded" if transferred_count > 0 else "skipped:hash_match"
        ref = FileReference(
            local_path=str(dest_dir),
            storage_path=prefix,
            is_durable=True,
            file_count=len(data_objects),
        )
        return DownloadOutput(ref=ref, synced=transferred_count > 0, reason=reason)
