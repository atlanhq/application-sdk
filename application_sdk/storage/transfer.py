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

This approach is backend-agnostic (no reliance on ETag formats) and works
identically for single files and directories.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

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


async def _get_remote_sha256(store: "ObjectStore", key: str) -> str | None:
    """Fetch the stored SHA-256 sidecar for *key*, or ``None`` if absent."""
    from application_sdk.storage.ops import _get_bytes

    data = await _get_bytes(_sidecar_key(key), store, normalize=False)
    return data.decode() if data else None


async def _put_remote_sha256(store: "ObjectStore", key: str, digest: str) -> None:
    """Write the SHA-256 sidecar for *key*."""
    from application_sdk.storage.ops import _put

    await _put(_sidecar_key(key), digest.encode(), store, normalize=False)


async def _upload_one(
    store: "ObjectStore",
    local_file: Path,
    store_key: str,
    *,
    skip_if_exists: bool,
) -> tuple[bool, str]:
    """Upload a single file.  Returns ``(transferred, reason)``."""
    from application_sdk.storage.ops import upload_file

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


async def _download_one(
    store: "ObjectStore",
    store_key: str,
    local_file: Path,
    *,
    skip_if_exists: bool,
) -> tuple[bool, str]:
    """Download a single file.  Returns ``(transferred, reason)``."""
    from application_sdk.storage.ops import download_file

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
    "/private/var/",
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
    if ".." in path.parts:
        raise ValueError(f"Path traversal detected in upload path: {path!r}")

    resolved = path.resolve()
    resolved_str = str(resolved)

    if resolved_str.startswith(_SENSITIVE_SYSTEM_PREFIXES):
        raise ValueError(f"Upload from sensitive system path blocked: {path!r}")

    if any(part in _SENSITIVE_DIR_NAMES for part in resolved.parts):
        raise ValueError(f"Upload from sensitive directory blocked: {path!r}")

    if resolved.is_file() and resolved.name.startswith(_SENSITIVE_FILE_PREFIXES):
        raise ValueError(f"Upload of sensitive file blocked: {path!r}")

    # User-defined blocked paths via ATLAN_UPLOAD_FILE_BLOCKED_PATHS (comma-separated).
    # Each entry is matched as a substring against the full resolved path.
    # e.g. ATLAN_UPLOAD_FILE_BLOCKED_PATHS="/custom/secrets/,.vault,.credentials"
    user_blocked = _parse_blocked_paths()
    if any(pattern in resolved_str for pattern in user_blocked):
        raise ValueError(f"Upload blocked by ATLAN_UPLOAD_FILE_BLOCKED_PATHS: {path!r}")


async def upload(
    local_path: str,
    storage_path: str | None = None,
    *,
    storage_subdir: str | None = None,
    skip_if_exists: bool = False,
    store: "ObjectStore | None" = None,
    _app_prefix: str = "",
    _tier: StorageTier = StorageTier.RETAINED,
) -> "UploadOutput":
    """Upload a local file or directory to the object store.

    When *storage_path* is ``None`` and *_app_prefix* is provided the key /
    prefix is auto-namespaced as ``{_app_prefix}/{filename}`` (files) or
    ``{_app_prefix}/`` (directories).

    When *storage_subdir* is set and *storage_path* is ``None``, the subdir name is
    appended to _app_prefix so files land at ``{_app_prefix}/{storage_subdir}/...``.
    This preserves the directory name in the object store path.

    Args:
        local_path: Local file or directory to upload.
        storage_path: Destination key or prefix override.  Takes priority
            over *storage_subdir* and *_app_prefix* when set.
        storage_subdir: Subdirectory name appended to the auto-generated run prefix.
            Ignored when *storage_path* is set.
        skip_if_exists: Skip files whose SHA-256 matches the stored sidecar.
        store: Object store to use, or ``None`` to resolve from infrastructure.
        _app_prefix: Internal prefix injected by the ``App.upload`` task.

    Returns:
        :class:`~application_sdk.contracts.storage.UploadOutput`
    """
    from application_sdk.contracts.storage import UploadOutput
    from application_sdk.storage.ops import _resolve_store, normalize_key

    resolved = _resolve_store(store)
    src = Path(local_path)

    # Block sensitive paths
    _validate_upload_path(src)

    if storage_subdir:
        from pathlib import PurePosixPath

        cleaned = storage_subdir.strip("/")
        if not cleaned or ".." in PurePosixPath(cleaned).parts or "\x00" in storage_subdir:
            raise ValueError(
                f"storage_subdir must not contain path traversal segments: {storage_subdir!r}"
            )
        storage_subdir = cleaned

    if src.is_file():
        # ── Single file ────────────────────────────────────────────────────
        if storage_path is not None:
            key = normalize_key(storage_path)
        elif _app_prefix and storage_subdir:
            key = f"{_app_prefix}/{normalize_key(storage_subdir)}/{src.name}"
        elif _app_prefix:
            key = f"{_app_prefix}/{src.name}"
        else:
            key = src.name

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

    elif src.is_dir():
        # ── Directory ──────────────────────────────────────────────────────
        if storage_path is not None:
            prefix = normalize_key(storage_path)
        elif _app_prefix and storage_subdir:
            prefix = f"{_app_prefix}/{normalize_key(storage_subdir)}"
        elif _app_prefix:
            prefix = _app_prefix
        else:
            prefix = src.name

        files = [p for p in src.rglob("*") if p.is_file()]
        transferred_count = 0
        for file_path in files:
            relative = str(file_path.relative_to(src)).replace(os.sep, "/")
            key = f"{prefix}/{relative}" if prefix else relative
            ok, _ = await _upload_one(
                resolved, file_path, key, skip_if_exists=skip_if_exists
            )
            if ok:
                transferred_count += 1

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

    else:
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"local_path does not exist or is not a file/directory: {local_path}"
        )


async def download(
    storage_path: str,
    local_path: str | None = None,
    *,
    skip_if_exists: bool = False,
    store: "ObjectStore | None" = None,
) -> "DownloadOutput":
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
    from application_sdk.contracts.storage import DownloadOutput
    from application_sdk.storage.ops import _resolve_store, list_keys, normalize_key

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
        if local_path is not None:
            dest = Path(local_path)
        else:
            suffix = Path(norm_path).suffix or ""
            _, tmp = tempfile.mkstemp(suffix=suffix)
            dest = Path(tmp)

        transferred, reason = await _download_one(
            resolved, norm_path, dest, skip_if_exists=skip_if_exists
        )
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
            rel = key[len(strip) :] if key.startswith(strip) else key
            local_file = dest_dir / rel
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
