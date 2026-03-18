"""FileReference persist / materialize operations.

A ``FileReference`` can be in one of two states:

* **Ephemeral** (``is_durable=False``): the data lives only on the local
  filesystem (``local_path`` is set).  This is safe to pass within a single
  activity, but cannot survive a Temporal payload round-trip because the
  remote worker won't have the file.

* **Durable** (``is_durable=True``): the data has been uploaded to the
  object store (``storage_path`` is set).  The reference can be serialised
  into a Temporal payload and materialised on any worker.

``persist_file_reference`` transitions ephemeral → durable.
``materialize_file_reference`` transitions durable → local (downloads the
file to a temp path when ``local_path`` is absent or cannot be verified).

SHA-256 sidecars
----------------
Both functions maintain a ``{path}.sha256`` sidecar alongside every file:

* **persist**: computes sha256 of the local file via streaming upload,
  writes ``{storage_path}.sha256`` to the store, and writes
  ``{local_path}.sha256`` locally.
* **materialize**: before downloading a single file, checks whether the
  local file (if present) already matches the stored sidecar.  If so,
  writes the local sidecar and returns without re-downloading.  Otherwise
  downloads the file via streaming, verifies integrity against the stored
  sidecar (if available), then writes the local sidecar.

For directory references, the fast path is skipped — materialisation always
re-lists the prefix and re-downloads any missing or changed files.

The conservative default is: **no stored sidecar → re-download**.  Once a
sidecar exists, subsequent calls on the same worker skip the download
entirely.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk.contracts.types import FileReference

if TYPE_CHECKING:
    from obstore.store import ObjectStore

logger = logging.getLogger(__name__)


def _make_storage_path(ref: FileReference) -> str:
    """Generate a unique storage path for a single-file FileReference."""
    suffix = ""
    if ref.local_path:
        suffix = Path(ref.local_path).suffix
    return f"file_refs/{uuid.uuid4().hex}{suffix}"


def _make_storage_prefix(ref: FileReference) -> str:
    """Generate a unique storage prefix for a directory FileReference."""
    return f"file_refs/{uuid.uuid4().hex}/"


def _sha256_hex_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of *path* using chunked reads.

    Reads in 1 MiB chunks so memory usage is constant regardless of file size.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _get_stored_sidecar(storage_path: str, store: "ObjectStore") -> str | None:
    """Fetch the stored sha256 sidecar for *storage_path*, or None if absent."""
    from application_sdk.storage.ops import _get_bytes

    try:
        raw = await _get_bytes(storage_path + ".sha256", store)
        return raw.decode().strip() if raw else None
    except Exception:
        return None


def _write_local_sidecar(local_path: str, sha256: str) -> None:
    """Write a local ``.sha256`` sidecar next to *local_path*."""
    try:
        Path(local_path + ".sha256").write_text(sha256)
    except Exception:
        pass  # sidecar is best-effort; don't fail the main operation


async def persist_file_reference(
    store: "ObjectStore",
    ref: FileReference,
    *,
    key: str | None = None,
) -> FileReference:
    """Upload the local file or directory referenced by *ref* to *store*.

    For single files, performs a streaming upload and writes a
    ``{storage_path}.sha256`` sidecar to the store and a
    ``{local_path}.sha256`` sidecar locally so that subsequent
    ``materialize_file_reference`` calls can verify integrity without
    re-downloading.

    For directories, walks the directory tree, uploads each file under a
    generated prefix, and writes per-file sidecars.

    Args:
        store: Destination obstore store.
        ref: An ephemeral ``FileReference`` with ``local_path`` set.
        key: Override the generated storage path (single files only).

    Returns:
        A new durable ``FileReference`` (``is_durable=True``) pointing to
        the same data in the store.

    Raises:
        StorageError: If ``ref.local_path`` is ``None`` or the upload fails.
    """
    from application_sdk.storage.errors import StorageError
    from application_sdk.storage.ops import _put, upload_file

    if ref.is_durable:
        return ref  # already persisted — nothing to do

    if ref.local_path is None:
        raise StorageError(
            "Cannot persist FileReference: local_path is None",
            key=key,
        )

    local = Path(ref.local_path)

    if local.is_dir():
        # ── Directory upload ───────────────────────────────────────────────
        prefix = _make_storage_prefix(ref)

        files = [p for p in local.rglob("*") if p.is_file()]
        logger.debug(
            "persist_file_reference: local_path=%s is directory, found %d files to upload to prefix=%s",
            ref.local_path,
            len(files),
            prefix,
        )

        for file_path in files:
            relative = str(file_path.relative_to(local)).replace(os.sep, "/")
            file_key = f"{prefix}{relative}"
            sha256 = await upload_file(file_key, file_path, store, normalize=False)
            # Write remote sidecar for this file.
            try:
                await _put(
                    file_key + ".sha256", sha256.encode(), store, normalize=False
                )
            except Exception:
                pass  # sidecar is best-effort

        return FileReference(
            local_path=ref.local_path,
            is_durable=True,
            storage_path=prefix,
            file_count=len(files),
        )

    else:
        # ── Single file upload ─────────────────────────────────────────────
        storage_path = key or _make_storage_path(ref)

        sha256 = await upload_file(storage_path, local, store, normalize=False)

        # Upload sha256 sidecar to store so any worker can verify the file.
        try:
            await _put(
                storage_path + ".sha256", sha256.encode(), store, normalize=False
            )
        except Exception:
            pass  # sidecar is best-effort; don't fail the persist

        # Write local sidecar so this worker can skip re-downloads immediately.
        _write_local_sidecar(ref.local_path, sha256)

        return FileReference(
            local_path=ref.local_path,
            is_durable=True,
            storage_path=storage_path,
        )


async def materialize_file_reference(
    store: "ObjectStore",
    ref: FileReference,
    *,
    local_dir: str | None = None,
) -> FileReference:
    """Download the file or directory referenced by *ref* from *store* locally.

    Uses ``list_keys`` to determine whether *ref* is a single file or a
    directory prefix, then downloads accordingly.

    **Single file**: if ``ref.local_path`` already exists on disk AND the
    stored sha256 sidecar confirms the file is intact, the local sidecar is
    (re-)written and the function returns without downloading.

    **Directory**: fast path is always skipped; all files under the prefix
    are re-listed and downloaded.

    Args:
        store: Source obstore store.
        ref: A durable ``FileReference`` with ``storage_path`` set.
        local_dir: Optional directory for temp files/dirs (uses the system
            temp dir if ``None``).

    Returns:
        A ``FileReference`` with ``local_path`` pointing to the verified
        file or directory on the local filesystem.

    Raises:
        StorageNotFoundError: If the key does not exist in the store.
        StorageError: If the downloaded data does not match the stored sidecar.
    """
    from application_sdk.storage.errors import StorageError, StorageNotFoundError
    from application_sdk.storage.ops import download_file, list_keys

    if not ref.is_durable or ref.storage_path is None:
        return ref  # nothing to materialise

    # Determine single-file vs directory by listing sub-keys under the path.
    all_keys = await list_keys(ref.storage_path, store)
    data_keys = [k for k in all_keys if not k.endswith(".sha256")]

    logger.debug(
        "materialize_file_reference: storage_path=%s, sub_keys=%d (after sidecar filtering)",
        ref.storage_path,
        len(data_keys),
    )

    if not data_keys:
        # ── Single file ────────────────────────────────────────────────────

        # Fast path: local file exists — validate before deciding to download.
        stored_hash: str | None = None
        if ref.local_path is not None and Path(ref.local_path).exists():
            local_hash = _sha256_hex_file(Path(ref.local_path))
            stored_hash = await _get_stored_sidecar(ref.storage_path, store)

            if stored_hash is not None:
                if local_hash == stored_hash:
                    # File is intact — stamp local sidecar and reuse.
                    _write_local_sidecar(ref.local_path, local_hash)
                    return ref
                # Hash mismatch — file is corrupt; fall through to re-download.
            # else: no stored sidecar → conservative: re-download (cannot verify).

        # Determine output path.
        if ref.local_path is not None:
            out_path = ref.local_path
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        else:
            suffix = Path(ref.storage_path).suffix or ""
            if local_dir:
                Path(local_dir).mkdir(parents=True, exist_ok=True)
                fd, out_path = tempfile.mkstemp(suffix=suffix, dir=local_dir)
            else:
                fd, out_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)  # close immediately; download_file will overwrite

        sha256 = await download_file(
            ref.storage_path, out_path, store, compute_hash=True, normalize=False
        )
        if sha256 is None:
            raise StorageNotFoundError(
                f"FileReference storage path not found in store: {ref.storage_path}",
                key=ref.storage_path,
            )

        # Verify against stored sidecar (reuse fetched value if already retrieved).
        if stored_hash is None:
            stored_hash = await _get_stored_sidecar(ref.storage_path, store)
        if stored_hash is not None and sha256 != stored_hash:
            raise StorageError(
                f"SHA-256 mismatch for {ref.storage_path}: "
                f"downloaded={sha256}, stored={stored_hash}",
                key=ref.storage_path,
            )

        _write_local_sidecar(out_path, sha256)

        return FileReference(
            local_path=out_path,
            is_durable=True,
            storage_path=ref.storage_path,
        )

    else:
        # ── Directory / prefix ─────────────────────────────────────────────

        if ref.local_path is not None:
            local_directory = ref.local_path
        elif local_dir is not None:
            local_directory = local_dir
        else:
            local_directory = tempfile.mkdtemp()

        Path(local_directory).mkdir(parents=True, exist_ok=True)

        prefix = ref.storage_path.rstrip("/") + "/"
        for key in data_keys:
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            dest = os.path.join(local_directory, rel)
            await download_file(key, dest, store, compute_hash=False, normalize=False)

        return FileReference(
            local_path=local_directory,
            is_durable=True,
            storage_path=ref.storage_path,
            file_count=len(data_keys),
        )
