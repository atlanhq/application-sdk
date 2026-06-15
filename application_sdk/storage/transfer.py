"""Lower-level upload / download implementation used by the App tasks.

These functions can also be called directly from within an existing ``@task``
when task-wrapping is not desired (edge-case opt-in).

SHA-256-based deduplication
---------------------------
Every uploaded object gets a tiny sidecar ``{key}.sha256`` stored alongside
it in the object store.  On subsequent uploads (``skip_if_exists=True``) the
local file hash is compared against the sidecar; the upload is skipped when
they match.  The same sidecar is used during downloads to verify integrity
and skip unchanged local files.

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
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk.common._listing import safe_list_directory
from application_sdk.constants import MAX_CONCURRENT_STORAGE_TRANSFERS
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from obstore.store import ObjectStore

    from application_sdk.contracts.storage import DownloadOutput, UploadOutput


_SHA256_SUFFIX = ".sha256"


def _is_sidecar(key: str) -> bool:
    return key.endswith(_SHA256_SUFFIX)


def _sidecar_key(key: str) -> str:
    return key + _SHA256_SUFFIX


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a local file (streaming, 64 KiB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _get_remote_sha256(store: ObjectStore, key: str) -> str | None:
    """Fetch the stored SHA-256 sidecar for *key*, or ``None`` if absent."""
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _get_bytes,
    )

    data = await _get_bytes(_sidecar_key(key), store, normalize=False)
    return data.decode() if data else None


async def _put_remote_sha256(store: ObjectStore, key: str, digest: str) -> None:
    """Write the SHA-256 sidecar for *key*."""
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _put,
    )

    await _put(_sidecar_key(key), digest.encode(), store, normalize=False)


async def _upload_one(
    store: ObjectStore,
    local_file: Path,
    store_key: str,
    *,
    skip_if_exists: bool,
) -> tuple[bool, str]:
    """Upload a single file.  Returns ``(transferred, reason)``."""
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        upload_file,
    )

    if skip_if_exists:
        local_digest = _sha256_file(local_file)
        remote_digest = await _get_remote_sha256(store, store_key)
        if remote_digest == local_digest:
            return False, "skipped:hash_match"
        sha256 = await upload_file(store_key, local_file, store, normalize=False)
    else:
        sha256 = await upload_file(store_key, local_file, store, normalize=False)

    await _put_remote_sha256(store, store_key, sha256)
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
    source_digest = await _get_remote_sha256(source_store, source_key)
    if source_digest is None:
        return False
    target_digest = await _get_remote_sha256(target_store, target_key)
    return source_digest == target_digest


async def _upload_from_store(
    source_store: ObjectStore,
    source_key: str,
    target_store: ObjectStore,
    target_key: str,
) -> tuple[bool, str]:
    """Upload a single file from *source_store* to *target_store*.

    Implements steps 1 and 3 of the three-step upload strategy:

    * Step 1 — cross-store SHA-256 dedup: skips the transfer when both stores
      already hold the same content at their respective keys.
    * Step 3 — deployment-store fallback: downloads to a temporary local file
      and re-uploads to the target, writing the SHA-256 sidecar.

    Returns ``(transferred, reason)``.
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        download_file,
        upload_file,
    )

    if await _cross_store_sha256_match(
        source_store, source_key, target_store, target_key
    ):
        return False, "skipped:hash_match"

    fd, tmp_path_str = tempfile.mkstemp()
    os.close(fd)
    tmp = Path(tmp_path_str)
    try:
        await download_file(source_key, tmp, source_store, normalize=False)
        sha256 = await upload_file(target_key, tmp, target_store, normalize=False)
        await _put_remote_sha256(target_store, target_key, sha256)
        return True, "uploaded"
    finally:
        tmp.unlink(missing_ok=True)


async def _download_one(
    store: ObjectStore,
    store_key: str,
    local_file: Path,
    *,
    skip_if_exists: bool,
) -> tuple[bool, str]:
    """Download a single file.  Returns ``(transferred, reason)``."""
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        download_file,
    )

    if skip_if_exists and local_file.exists():
        remote_digest = await _get_remote_sha256(store, store_key)
        if remote_digest is not None:
            local_digest = _sha256_file(local_file)
            if local_digest == remote_digest:
                return False, "skipped:hash_match"

    local_file.parent.mkdir(parents=True, exist_ok=True)
    await download_file(store_key, local_file, store, normalize=False)
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
        prefix = _derive_target_key(
            storage_path,
            _app_prefix,
            storage_subdir,
            src.name,
            normalize_key,
            append_leaf=False,
        )
        # PART-1148: safe_list_directory composes an fsync barrier (Darwin
        # F_FULLFSYNC / Linux fsync) with an os.scandir-based recursion
        # that surfaces OSError instead of swallowing it like pathlib's
        # Path.rglob does (cpython#146646). Closes the silent
        # file_count=0 silent-failure mode observed under concurrent
        # write-then-list load on macOS APFS. See ADR-0015.
        #
        # asyncio.to_thread offloads the blocking I/O (open, fsync,
        # scandir) off the event loop — SDK convention for sync syscalls
        # inside async paths.
        files = await asyncio.to_thread(safe_list_directory, src)
        if raise_on_empty and not files:
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

        # conformance: ignore[E010] results checked immediately below: errs filters BaseException, first is re-raised and rest are logged
        results = await asyncio.gather(
            *[_bounded_upload(fp, k) for fp, k in zip(files, keys)],
            return_exceptions=True,
        )
        errs = [r for r in results if isinstance(r, BaseException)]
        if errs:
            for extra in errs[1:]:
                _logger.error("concurrent upload failure (suppressed)", exc_info=extra)
            raise errs[0]
        n = sum(1 for ok in results if ok)
        reason = "uploaded" if n > 0 else "skipped:hash_match"
        return _make_upload_output(
            str(src), prefix, len(files), _tier, n, reason, is_dir=True
        )

    elif (
        _source_ref is not None
        and _source_ref.storage_path
        and source_resolved is not None
    ):
        # ── Deployment-store fallback (local path absent) ──────────────────
        from pathlib import PurePosixPath  # noqa: PLC0415 — stdlib; lazy use only

        from application_sdk.storage.batch import list_keys  # noqa: PLC0415

        source_norm = normalize_key(_source_ref.storage_path)
        if source_norm and ".." in PurePosixPath(source_norm).parts:
            from application_sdk.storage.errors import (  # noqa: PLC0415
                UnsafeUploadPathError,
            )

            raise UnsafeUploadPathError(unsafe_path=_source_ref.storage_path)

        source_dir_prefix = source_norm.rstrip("/") + "/"
        data_dir_keys = [
            k
            for k in await list_keys(
                source_dir_prefix, source_resolved, normalize=False
            )
            if not _is_sidecar(k)
        ]

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
    from application_sdk.storage.batch import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        list_keys,
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
            transferred, reason = await _download_one(
                resolved, norm_path, dest, skip_if_exists=skip_if_exists
            )
        # conformance: ignore[E004] cleanup-only handler that always re-raises; no logging needed here
        except BaseException:
            # Don't leave an empty temp file behind on download failure
            # (BLDX-1155 #5).
            if owns_temp:
                try:
                    dest.unlink(missing_ok=True)
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
        all_keys = await list_keys(prefix, resolved, normalize=False)
        # Exclude SHA-256 sidecars from the listing.
        data_keys = [k for k in all_keys if not _is_sidecar(k)]

        if local_path is not None:
            dest_dir = Path(local_path)
        else:
            dest_dir = Path(tempfile.mkdtemp())

        dest_dir.mkdir(parents=True, exist_ok=True)
        strip = prefix

        transferred_count = 0
        for key in data_keys:
            rel = key.removeprefix(strip)
            # Reject keys whose resolved path escapes dest_dir (e.g. via ".." segments).
            local_file = _safe_join_under(dest_dir, rel)
            ok, _ = await _download_one(
                resolved, key, local_file, skip_if_exists=skip_if_exists
            )
            if ok:
                transferred_count += 1

        reason = "downloaded" if transferred_count > 0 else "skipped:hash_match"
        ref = FileReference(
            local_path=str(dest_dir),
            storage_path=prefix,
            is_durable=True,
            file_count=len(data_keys),
        )
        return DownloadOutput(ref=ref, synced=transferred_count > 0, reason=reason)
