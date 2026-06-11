"""Local-filesystem objectstore integration matrix — round trips and error branches.

Exercises the public storage API (``ops`` + ``batch``) against a real obstore
``LocalStore``: no mocks, no cloud credentials, no Temporal server.

Covers the gaps called out in the test-quality audit:
* single-file round trips, including the parallel range-GET (chunked) path
  for files above the 16 MiB chunk threshold
* directory (prefix) round trips
* typed-error branches (``StorageNotFoundError`` and friends) and the exact
  current contract for missing keys and empty prefixes
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from application_sdk.storage.batch import (
    delete_prefix,
    download_prefix,
    list_keys,
    upload_prefix,
)
from application_sdk.storage.errors import StorageNotFoundError
from application_sdk.storage.ops import (
    delete,
    download_file,
    download_file_chunked,
    exists,
    get_file_size,
    upload_file,
)

# download_file_chunked dispatches to parallel range-GETs strictly above this.
CHUNK_THRESHOLD = 16 * 1024 * 1024

ONE_MIB_BLOCK = bytes(range(256)) * 4096  # 1 MiB of deterministic content


def _write_file_of_mib(path: Path, mib: int) -> str:
    """Write a deterministic *mib*-MiB file and return its SHA-256 hex digest."""
    h = hashlib.sha256()
    with path.open("wb") as fh:
        for _ in range(mib):
            fh.write(ONE_MIB_BLOCK)
            h.update(ONE_MIB_BLOCK)
    return h.hexdigest()


# ------------------------------------------------------------------
# Single-file round trips
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_download_roundtrip_small_file(local_store, tmp_path):
    """upload_file → download_file round trip preserves content and digest."""
    src = tmp_path / "small.txt"
    src.write_text("local objectstore matrix")

    up_sha = await upload_file("matrix/small.txt", src, local_store)
    assert up_sha is not None and len(up_sha) == 64

    dest = tmp_path / "small_out.txt"
    dl_sha = await download_file(
        "matrix/small.txt", dest, local_store, compute_hash=True
    )
    assert dest.read_text() == "local objectstore matrix"
    assert dl_sha == up_sha


@pytest.mark.integration
async def test_chunked_download_roundtrip_above_threshold(local_store, tmp_path):
    """A >16 MiB file takes the parallel range-GET path and survives intact."""
    src = tmp_path / "big.bin"
    src_sha = _write_file_of_mib(src, 17)  # 17 MiB > 16 MiB chunk threshold
    assert src.stat().st_size > CHUNK_THRESHOLD

    up_sha = await upload_file("matrix/big.bin", src, local_store)
    assert up_sha == src_sha

    dest = tmp_path / "big_out.bin"
    dl_sha = await download_file_chunked("matrix/big.bin", dest, local_store)

    assert dest.stat().st_size == src.stat().st_size
    assert dl_sha == src_sha
    assert dest.read_bytes() == src.read_bytes()


@pytest.mark.integration
async def test_chunked_download_small_file_falls_through_to_streaming(
    local_store, tmp_path
):
    """Files at or below the chunk size use the single-stream path, same result."""
    src = tmp_path / "medium.bin"
    src_sha = _write_file_of_mib(src, 1)

    await upload_file("matrix/medium.bin", src, local_store)

    dest = tmp_path / "medium_out.bin"
    dl_sha = await download_file_chunked("matrix/medium.bin", dest, local_store)
    assert dl_sha == src_sha
    assert dest.read_bytes() == src.read_bytes()


# ------------------------------------------------------------------
# Prefix (directory) round trips
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_prefix_download_prefix_roundtrip(local_store, tmp_path):
    """upload_prefix → list_keys → download_prefix preserves the directory tree."""
    src_dir = tmp_path / "tree"
    (src_dir / "nested").mkdir(parents=True)
    (src_dir / "root.txt").write_text("root")
    (src_dir / "nested" / "leaf.txt").write_text("leaf")

    uploaded = await upload_prefix(src_dir, "matrix/tree", local_store)
    assert sorted(uploaded) == [
        "matrix/tree/nested/leaf.txt",
        "matrix/tree/root.txt",
    ]

    keys = await list_keys("matrix/tree", local_store)
    assert keys == ["matrix/tree/nested/leaf.txt", "matrix/tree/root.txt"]

    dest_dir = tmp_path / "restored"
    paths = await download_prefix("matrix/tree", dest_dir, local_store)
    assert len(paths) == 2
    assert (dest_dir / "matrix/tree/root.txt").read_text() == "root"
    assert (dest_dir / "matrix/tree/nested/leaf.txt").read_text() == "leaf"


@pytest.mark.integration
async def test_delete_prefix_returns_deleted_count(local_store, tmp_path):
    """delete_prefix removes every object under the prefix and reports the count."""
    for i in range(3):
        src = tmp_path / f"f{i}.txt"
        src.write_text(f"data-{i}")
        await upload_file(f"matrix/wipe/f{i}.txt", src, local_store)
    keeper = tmp_path / "keep.txt"
    keeper.write_text("keep")
    await upload_file("matrix/keep/keep.txt", keeper, local_store)

    assert await delete_prefix("matrix/wipe", local_store) == 3
    assert await list_keys("matrix/wipe", local_store) == []
    # Sibling prefix is untouched — listing never bleeds across prefixes.
    assert await list_keys("matrix/keep", local_store) == ["matrix/keep/keep.txt"]


# ------------------------------------------------------------------
# Error branches — assert the CURRENT typed-error contract precisely
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_download_missing_key_raises_typed_not_found(local_store, tmp_path):
    """download_file of a missing key raises StorageNotFoundError with evidence."""
    with pytest.raises(StorageNotFoundError) as excinfo:
        await download_file("missing/nope.txt", tmp_path / "out", local_store)

    err = excinfo.value
    assert err.key == "missing/nope.txt"
    assert err.code == "STORAGE_NOT_FOUND"
    assert err.default_retryable is False
    # Failed download must not leave a partial file behind for this branch.
    assert not (tmp_path / "out").exists()


@pytest.mark.integration
async def test_chunked_download_missing_key_raises_typed_not_found(
    local_store, tmp_path
):
    """download_file_chunked surfaces the same typed error as the streaming path."""
    with pytest.raises(StorageNotFoundError) as excinfo:
        await download_file_chunked("missing/nope.bin", tmp_path / "out", local_store)
    assert excinfo.value.key == "missing/nope.bin"


@pytest.mark.integration
async def test_missing_key_probes_return_falsy_not_raise(local_store):
    """exists/delete/get_file_size on a missing key return falsy values, not errors."""
    assert await exists("missing/nope.txt", local_store) is False
    assert await delete("missing/nope.txt", local_store) is False
    assert await get_file_size("missing/nope.txt", local_store) is None


@pytest.mark.integration
async def test_upload_missing_local_file_raises_builtin_error(local_store, tmp_path):
    """upload_file with a nonexistent local path raises FileNotFoundError.

    Current contract: the local stat() happens before any store I/O, so the
    builtin error propagates untyped (it is a caller bug, not a storage
    failure).  If this is ever wrapped in StorageError, update this test
    deliberately — apps may be matching on FileNotFoundError today.
    """
    with pytest.raises(FileNotFoundError):
        await upload_file(
            "matrix/never.txt", tmp_path / "no-such-file.txt", local_store
        )


@pytest.mark.integration
async def test_delete_then_download_raises_not_found(local_store, tmp_path):
    """A deleted key behaves exactly like a never-written key."""
    src = tmp_path / "gone.txt"
    src.write_text("ephemeral")
    await upload_file("matrix/gone.txt", src, local_store)

    assert await delete("matrix/gone.txt", local_store) is True
    with pytest.raises(StorageNotFoundError):
        await download_file("matrix/gone.txt", tmp_path / "gone_out.txt", local_store)


# ------------------------------------------------------------------
# Empty-prefix contracts — assert the CURRENT behavior precisely
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_empty_prefix_lists_entire_store(local_store, tmp_path):
    """list_keys("") is a full-store listing (current contract, relied upon)."""
    src = tmp_path / "x.txt"
    src.write_text("x")
    await upload_file("a/one.txt", src, local_store)
    await upload_file("b/two.txt", src, local_store)

    assert await list_keys("", local_store) == ["a/one.txt", "b/two.txt"]


@pytest.mark.integration
async def test_empty_prefix_download_and_delete_cover_entire_store(
    local_store, tmp_path
):
    """download_prefix("") and delete_prefix("") operate on the whole store."""
    src = tmp_path / "y.txt"
    src.write_text("y")
    await upload_file("a/one.txt", src, local_store)
    await upload_file("b/two.txt", src, local_store)

    dest = tmp_path / "everything"
    paths = await download_prefix("", dest, local_store)
    assert sorted(Path(p).relative_to(dest).as_posix() for p in paths) == [
        "a/one.txt",
        "b/two.txt",
    ]

    assert await delete_prefix("", local_store) == 2
    assert await list_keys("", local_store) == []


@pytest.mark.integration
async def test_nonexistent_prefix_is_empty_not_error(local_store, tmp_path):
    """Listing/downloading/deleting a prefix with no objects is a quiet no-op."""
    assert await list_keys("never-written", local_store) == []
    assert await download_prefix("never-written", tmp_path / "dl", local_store) == []
    assert await delete_prefix("never-written", local_store) == 0
