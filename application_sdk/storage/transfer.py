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

from application_sdk.constants import MAX_CONCURRENT_STORAGE_TRANSFERS
from application_sdk.contracts.types import FileReference, StorageTier

if TYPE_CHECKING:
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


async def upload(
    local_path: str,
    storage_path: str | None = None,
    *,
    storage_subdir: str | None = None,
    skip_if_exists: bool = False,
    raise_on_empty: bool = False,
    store: ObjectStore | None = None,
    source_ref: FileReference | None = None,
    source_store: ObjectStore | None = None,
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

    Three-step upload strategy (when *source_ref* and *source_store* are supplied):

    1. **Cross-store SHA-256 dedup** — compare deployment-store sidecar against
       upstream sidecar; skip if they match (idempotent retry, zero bytes
       transferred).  Calling ``App.upload()`` twice for the same file short-
       circuits on the second call once the file is present in the target store.
    2. **Local upload** — if *local_path* exists on this pod, upload directly.
    3. **Deployment-store fallback** — if *local_path* is absent (cross-pod
       KEDA-scaled SDR worker or writer-deleted by ``use_consolidation=True``),
       stream from *source_store* to the target.

    ``App.upload()`` automatically derives *source_ref* from *local_path* and
    always passes ``source_store=self.context.storage``, so all existing call
    sites gain the fallback for free without API changes.

    Args:
        local_path: Local file or directory to upload.
        storage_path: Destination key or prefix override.  Takes priority
            over *storage_subdir* and *_app_prefix* when set.
        storage_subdir: Subdirectory name appended to the auto-generated run prefix.
            Ignored when *storage_path* is set.
        skip_if_exists: Skip files whose local SHA-256 matches the stored sidecar
            (step 2 only; the cross-store dedup in step 1 is unconditional).
        raise_on_empty: When ``True``, raise ``StorageEmptyUploadError`` if
            *local_path* is a directory that contains zero files. Opt-in
            fail-loud for connectors where empty output indicates a bug
            (see BLDX-1255). Defaults to ``False`` to preserve historical
            silent-zero behavior that incremental extractors rely on.
        store: Target object store (upstream in SDR, deployment otherwise), or
            ``None`` to resolve from infrastructure.
        source_ref: Pre-computed ``FileReference`` carrying both ``local_path``
            and ``storage_path`` for the deployment store.  Derived automatically
            by ``App.upload()`` from *local_path*; callers can also supply it
            directly (e.g. from a previous task output).
        source_store: Deployment-store binding used as the step-3 fallback
            source.  Always ``self.context.storage`` when called via
            ``App.upload()``.
        _app_prefix: Internal prefix injected by the ``App.upload`` task.
        max_concurrency: Maximum parallel uploads for directory mode
            (default :data:`~application_sdk.constants.MAX_CONCURRENT_STORAGE_TRANSFERS`).

    Returns:
        :class:`~application_sdk.contracts.storage.UploadOutput`

    Raises:
        StorageError: If *local_path* does not exist or is neither a file
            nor a directory, and no *source_ref* / *source_store* fallback
            is available.
        StorageEmptyUploadError: When *raise_on_empty* is ``True`` and
            *local_path* is a directory containing zero files
            (category=DATA_INTEGRITY, audience=APP_OWNER, retryable=False).
    """
    from application_sdk.contracts.storage import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        UploadOutput,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _resolve_store,
        normalize_key,
    )

    resolved = _resolve_store(store)
    source_resolved = _resolve_store(source_store) if source_store is not None else None
    src = Path(local_path)

    # Block sensitive paths (runs even when the path does not exist locally
    # so uploads to sensitive locations are refused regardless of fallback path).
    # Guard: only validate when local_path is non-empty — Path("") resolves to
    # CWD on Python/macOS, so validating or stat-ing it without this guard
    # causes is_dir() to return True and accidentally upload the working tree.
    if local_path:
        _validate_upload_path(src)

    if storage_subdir:
        from pathlib import (  # noqa: PLC0415 — stdlib pathlib; lazy use only
            PurePosixPath,
        )

        cleaned = storage_subdir.strip("/")
        if (
            not cleaned
            or ".." in PurePosixPath(cleaned).parts
            or "\x00" in storage_subdir
        ):
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                UnsafeUploadPathError,
            )

            raise UnsafeUploadPathError(unsafe_path=storage_subdir)
        storage_subdir = cleaned

    if local_path and src.is_file():
        # ── Single file (step 2: local exists) ────────────────────────────
        if storage_path is not None:
            key = normalize_key(storage_path)
        elif _app_prefix and storage_subdir:
            key = f"{_app_prefix}/{normalize_key(storage_subdir)}/{src.name}"
        elif _app_prefix:
            key = f"{_app_prefix}/{src.name}"
        else:
            key = src.name

        # Step 1: cross-store SHA-256 dedup (skip if upstream already current).
        if (
            source_ref is not None
            and source_ref.storage_path
            and source_resolved is not None
        ) and await _cross_store_sha256_match(
            source_resolved, source_ref.storage_path, resolved, key
        ):
            ref = FileReference(
                local_path=str(src),
                storage_path=key,
                is_durable=True,
                file_count=1,
                tier=_tier,
            )
            return UploadOutput(ref=ref, synced=False, reason="skipped:hash_match")

        transferred, reason = await _upload_one(
            resolved, src, key, skip_if_exists=skip_if_exists
        )
        ref = FileReference(
            local_path=str(src),
            storage_path=key,
            is_durable=True,
            file_count=1,
            tier=_tier,
        )
        return UploadOutput(ref=ref, synced=transferred, reason=reason)

    elif local_path and src.is_dir():
        # ── Directory (step 2: local exists) ──────────────────────────────
        if storage_path is not None:
            prefix = normalize_key(storage_path)
        elif _app_prefix and storage_subdir:
            prefix = f"{_app_prefix}/{normalize_key(storage_subdir)}"
        elif _app_prefix:
            prefix = _app_prefix
        else:
            prefix = src.name

        sem = asyncio.Semaphore(max_concurrency)

        files = [p for p in src.rglob("*") if p.is_file()]

        if raise_on_empty and not files:
            # Opt-in fail-loud (BLDX-1255). Connectors hit by silent-zero
            # failures pass ``raise_on_empty=True`` so an empty extract
            # surfaces as a clear error here instead of propagating a
            # ``file_count=0`` ``UploadOutput`` that downstream publish
            # logic treats as success.
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
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

        # Build the per-file source key prefix for step-1 cross-store dedup.
        src_dir_prefix = ""
        if (
            source_ref is not None
            and source_ref.storage_path
            and source_resolved is not None
        ):
            src_dir_prefix = normalize_key(source_ref.storage_path).rstrip("/") + "/"

        async def _bounded_upload(file_path: Path, key: str) -> bool:
            async with sem:
                # Step 1: cross-store SHA-256 dedup per file.
                if src_dir_prefix and source_resolved is not None:
                    relative = str(file_path.relative_to(src)).replace(os.sep, "/")
                    src_file_key = src_dir_prefix + relative
                    if await _cross_store_sha256_match(
                        source_resolved, src_file_key, resolved, key
                    ):
                        return False
                # Step 2: upload from local.
                ok, _ = await _upload_one(
                    resolved, file_path, key, skip_if_exists=skip_if_exists
                )
                return ok

        keys = []
        for file_path in files:
            relative = str(file_path.relative_to(src)).replace(os.sep, "/")
            keys.append(f"{prefix}/{relative}" if prefix else relative)

        results = await asyncio.gather(
            *[_bounded_upload(fp, k) for fp, k in zip(files, keys)],
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        transferred_count = sum(1 for ok in results if ok)

        store_prefix = (prefix.rstrip("/") + "/") if prefix else ""
        reason = "uploaded" if transferred_count > 0 else "skipped:hash_match"
        ref = FileReference(
            local_path=str(src),
            storage_path=store_prefix,
            is_durable=True,
            file_count=len(files),
            tier=_tier,
        )
        return UploadOutput(ref=ref, synced=transferred_count > 0, reason=reason)

    elif (
        source_ref is not None
        and source_ref.storage_path
        and source_resolved is not None
    ):
        # ── Step 3: deployment-store fallback (local path absent) ─────────
        # local_path does not exist on this pod — stream from source_store.
        # Used when the file was written by a different worker (cross-pod SDR)
        # or deleted locally after upload (use_consolidation=True).
        from application_sdk.storage.batch import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            list_keys,
        )
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageEmptyUploadError,
        )

        source_norm = normalize_key(source_ref.storage_path)
        source_dir_prefix = source_norm.rstrip("/") + "/"

        # Probe the source store: sub-keys → directory; no sub-keys → single file.
        dir_keys = await list_keys(source_dir_prefix, source_resolved, normalize=False)
        data_dir_keys = [k for k in dir_keys if not _is_sidecar(k)]

        if data_dir_keys:
            # ── Directory fallback ─────────────────────────────────────────
            if storage_path is not None:
                fallback_prefix = normalize_key(storage_path)
            elif _app_prefix and storage_subdir:
                fallback_prefix = f"{_app_prefix}/{normalize_key(storage_subdir)}"
            elif _app_prefix:
                fallback_prefix = _app_prefix
            elif source_ref.local_path:
                fallback_prefix = Path(source_ref.local_path).name
            else:
                fallback_prefix = source_norm.rstrip("/").rsplit("/", 1)[-1]

            sem = asyncio.Semaphore(max_concurrency)

            async def _bounded_fallback_dir(source_key: str) -> bool:
                async with sem:
                    rel = source_key.removeprefix(source_dir_prefix)
                    tgt_key = f"{fallback_prefix}/{rel}" if fallback_prefix else rel
                    ok, _ = await _upload_from_store(
                        source_resolved, source_key, resolved, tgt_key
                    )
                    return ok

            results = await asyncio.gather(
                *[_bounded_fallback_dir(k) for k in data_dir_keys],
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                raise errors[0]
            transferred_count = sum(1 for ok in results if ok)

            store_prefix = (
                (fallback_prefix.rstrip("/") + "/") if fallback_prefix else ""
            )
            reason = "uploaded" if transferred_count > 0 else "skipped:hash_match"
            ref = FileReference(
                local_path=source_ref.local_path,
                storage_path=store_prefix,
                is_durable=True,
                file_count=len(data_dir_keys),
                tier=_tier,
            )
            return UploadOutput(ref=ref, synced=transferred_count > 0, reason=reason)

        else:
            # ── Single file fallback ───────────────────────────────────────
            if source_ref.local_path:
                local_name = Path(source_ref.local_path).name
            else:
                local_name = source_norm.rsplit("/", 1)[-1]

            if storage_path is not None:
                fallback_key = normalize_key(storage_path)
            elif _app_prefix and storage_subdir:
                fallback_key = (
                    f"{_app_prefix}/{normalize_key(storage_subdir)}/{local_name}"
                )
            elif _app_prefix:
                fallback_key = f"{_app_prefix}/{local_name}"
            else:
                fallback_key = local_name

            if raise_on_empty:
                raise StorageEmptyUploadError(
                    f"upload(source_ref.storage_path={source_ref.storage_path!r}): "
                    "deployment store returned zero files for the given key.",
                    local_path=source_ref.storage_path,
                )

            transferred, reason = await _upload_from_store(
                source_resolved, source_norm, resolved, fallback_key
            )
            ref = FileReference(
                local_path=source_ref.local_path,
                storage_path=fallback_key,
                is_durable=True,
                file_count=1,
                tier=_tier,
            )
            return UploadOutput(ref=ref, synced=transferred, reason=reason)

    else:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

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
        except BaseException:
            # Don't leave an empty temp file behind on download failure
            # (BLDX-1155 #5).
            if owns_temp:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass  # best-effort cleanup of partial download; re-raise follows
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
