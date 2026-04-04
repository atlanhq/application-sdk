"""Core async object storage operations over obstore.

All operations are async-only and delegate to obstore's async variants.
Streaming operations use asyncio.to_thread to prevent event loop starvation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

logger = logging.getLogger(__name__)

# Type alias for obstore store (any provider)
Store = Any


class ObjectMeta(TypedDict, total=False):
    """Object metadata returned by obstore operations."""

    path: str
    last_modified: datetime
    size: int
    e_tag: str | None
    version: str | None


async def get_bytes(store: Store, path: str) -> bytes:
    """Get object contents as bytes."""
    import obstore as obs

    result = await obs.get_async(store, path)
    data = await result.bytes_async()
    return bytes(data)


async def put(store: Store, path: str, data: bytes) -> Any:
    """Put object contents. obstore auto-handles multipart for large data."""
    import obstore as obs

    return await obs.put_async(store, path, data)


async def delete(store: Store, path: str | list[str]) -> None:
    """Delete object(s)."""
    import obstore as obs

    await obs.delete_async(store, path)


async def head(store: Store, path: str) -> ObjectMeta:
    """Get object metadata without downloading content."""
    import obstore as obs

    result: ObjectMeta = await obs.head_async(store, path)  # type: ignore[assignment]
    return result


async def list_objects(store: Store, prefix: str = "") -> AsyncIterator[dict]:
    """List objects with optional prefix. Handles pagination automatically."""
    import obstore as obs

    stream = obs.list(store, prefix=prefix)  # type: ignore[reportUnknownMemberType]
    async for chunk in stream:
        for meta in chunk:
            yield meta  # type: ignore[reportReturnType]


async def list_paths(store: Store, prefix: str = "") -> list[str]:
    """List object paths under a prefix. Returns flat list of path strings."""
    paths: list[str] = []
    async for meta in list_objects(store, prefix):
        paths.append(meta["path"])  # type: ignore[reportTypedDictNotRequiredAccess]
    return paths


async def exists(store: Store, path: str) -> bool:
    """Check if an object exists."""
    try:
        await head(store, path)
        return True
    except Exception:
        return False


# =============================================================================
# Streaming File Transfer (for large files)
# =============================================================================


async def stream_download(store: Store, key: str, local_path: Path) -> int:
    """Download from object storage to a local file using streaming I/O.

    Never loads the full file into memory.

    Returns:
        Total bytes downloaded.

    Raises:
        FileNotFoundError: If the object does not exist in the store.
    """
    import obstore as obs

    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = await obs.get_async(store, key)
    except Exception as e:
        error_msg = str(e).lower()
        if "not found" in error_msg or "no such" in error_msg or "404" in error_msg:
            raise FileNotFoundError(f"Object not found: {key}") from e
        raise

    total = 0
    with Path(local_path).open("wb") as f:
        async for chunk in result.stream():
            f.write(chunk)
            total += len(chunk)
    return total


def _upload_sync(store: Store, local_path: Path, key: str, chunk_size: int) -> int:
    """Blocking upload helper — runs inside asyncio.to_thread.

    Uses adaptive part size to stay under S3's 10,000-part limit.
    The final writer.close() can block for minutes on large files
    (observed 526s for 72GB), so running in a thread prevents
    heartbeat/health probe starvation.
    """
    import contextlib
    import math

    import obstore as obs

    file_size = local_path.stat().st_size
    min_part_size = math.ceil(file_size / 9_900) if file_size > 0 else chunk_size
    effective_chunk_size = max(chunk_size, min_part_size)

    writer = obs.open_writer(store, key, buffer_size=effective_chunk_size)
    total = 0
    try:
        with Path(local_path).open("rb") as f:
            while True:
                chunk = f.read(effective_chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
                total += len(chunk)
        writer.close()
    except Exception:
        with contextlib.suppress(Exception):
            writer.close()
        raise
    return total


async def stream_upload(
    store: Store,
    local_path: Path,
    key: str,
    *,
    chunk_size: int = 64 * 1024 * 1024,
) -> int:
    """Upload a local file to object storage using streaming I/O.

    Never loads the full file into memory. Uses adaptive part size
    to handle files of any size (S3 10,000-part limit).
    Dispatched to a thread to keep the event loop free.

    Returns:
        Total bytes uploaded.
    """
    import asyncio

    return await asyncio.to_thread(
        _upload_sync, store, Path(local_path), key, chunk_size
    )
