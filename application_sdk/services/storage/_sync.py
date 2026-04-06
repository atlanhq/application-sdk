"""Directory sync operations for object storage.

Provides high-level sync operations with timestamp-based incremental syncing:
- sync_from: Download objects to local directory
- sync_to: Upload local directory to storage
- download_all: Download all files matching optional extension filter
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

Store = Any


async def sync_from(
    store: Store,
    prefix: str,
    local_dir: Path,
    *,
    newer_only: bool = True,
) -> list[Path]:
    """Download objects from storage to local directory.

    Args:
        store: The object store to download from.
        prefix: Storage prefix to download from.
        local_dir: Local directory to download into.
        newer_only: If True, skip files where local is newer than remote.

    Returns:
        List of local paths that were downloaded.
    """
    import obstore as obs

    downloaded: list[Path] = []
    local_dir = Path(local_dir)
    prefix_normalized = prefix.rstrip("/")

    async for chunk in obs.list(store, prefix=prefix):
        for meta in chunk:
            object_path = meta["path"]
            if prefix_normalized:
                relative_path = object_path[len(prefix_normalized) :].lstrip("/")
            else:
                relative_path = object_path

            if not relative_path:
                continue

            local_path = local_dir / relative_path

            should_download = True
            if newer_only and local_path.exists():
                local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=UTC)
                remote_mtime = meta.get("last_modified")
                if remote_mtime:
                    if remote_mtime.tzinfo is None:
                        remote_mtime = remote_mtime.replace(tzinfo=UTC)
                    if local_mtime >= remote_mtime:
                        logger.debug(
                            "Skipping %s: local file is newer (local=%s, remote=%s)",
                            object_path,
                            local_mtime,
                            remote_mtime,
                        )
                        should_download = False

            if should_download:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                result = await obs.get_async(store, object_path)
                content = await result.bytes_async()
                local_path.write_bytes(content)
                logger.debug("Downloaded %s -> %s", object_path, local_path)
                downloaded.append(local_path)

    logger.info(
        "sync_from completed: downloaded %d files to %s", len(downloaded), local_dir
    )
    return downloaded


async def sync_to(
    store: Store,
    local_dir: Path,
    prefix: str,
    *,
    newer_only: bool = True,
) -> list[str]:
    """Upload local directory to storage.

    Args:
        store: The object store to upload to.
        local_dir: Local directory to upload from.
        prefix: Storage prefix to upload into.
        newer_only: If True, skip files where remote is newer than local.

    Returns:
        List of storage paths that were uploaded.
    """
    import obstore as obs

    uploaded: list[str] = []
    local_dir = Path(local_dir)

    prefix_normalized = prefix.rstrip("/")
    if prefix_normalized:
        prefix_normalized += "/"

    for local_path in local_dir.rglob("*"):
        if not local_path.is_file():
            continue

        relative_path = local_path.relative_to(local_dir)
        key = f"{prefix_normalized}{str(relative_path).replace(chr(92), '/')}"

        should_upload = True
        if newer_only:
            try:
                meta = await obs.head_async(store, key)
                remote_mtime = meta.get("last_modified")
                if remote_mtime:
                    local_mtime = datetime.fromtimestamp(
                        local_path.stat().st_mtime, tz=UTC
                    )
                    if remote_mtime.tzinfo is None:
                        remote_mtime = remote_mtime.replace(tzinfo=UTC)
                    if remote_mtime >= local_mtime:
                        logger.debug(
                            "Skipping %s: remote file is newer (local=%s, remote=%s)",
                            local_path,
                            local_mtime,
                            remote_mtime,
                        )
                        should_upload = False
            except Exception:
                logger.debug(
                    "Could not check remote metadata for %s — assuming absent, will upload",
                    key,
                    exc_info=True,
                )

        if should_upload:
            content = local_path.read_bytes()
            await obs.put_async(store, key, content)
            logger.debug("Uploaded %s -> %s", local_path, key)
            uploaded.append(key)

    logger.info("sync_to completed: uploaded %d files to %s", len(uploaded), prefix)
    return uploaded


async def download_all(
    store: Store,
    prefix: str,
    local_dir: Path,
    *,
    extension: str | None = None,
) -> list[Path]:
    """Download all files matching optional extension filter.

    Unlike sync_from, this always downloads regardless of timestamps.

    Args:
        store: The object store to download from.
        prefix: Storage prefix to search in.
        local_dir: Local directory to download into.
        extension: Optional file extension to filter by (e.g. ".csv").

    Returns:
        List of local paths that were downloaded.
    """
    import obstore as obs

    downloaded: list[Path] = []
    local_dir = Path(local_dir)
    prefix_normalized = prefix.rstrip("/")

    async for chunk in obs.list(store, prefix=prefix):
        for meta in chunk:
            object_path = meta["path"]
            if extension and not object_path.endswith(extension):
                continue

            if prefix_normalized:
                relative_path = object_path[len(prefix_normalized) :].lstrip("/")
            else:
                relative_path = object_path

            if not relative_path:
                continue

            local_path = local_dir / relative_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            result = await obs.get_async(store, object_path)
            content = await result.bytes_async()
            local_path.write_bytes(content)

            logger.debug("Downloaded %s -> %s", object_path, local_path)
            downloaded.append(local_path)

    logger.info("download_all completed: downloaded %d files", len(downloaded))
    return downloaded
