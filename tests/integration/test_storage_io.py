"""Integration tests for storage.ops and io/ module working together.

Verifies the full flow: upload → list → download_prefix (concurrent)
using a real local obstore (no mocks, no Temporal).
"""

from __future__ import annotations

import warnings

import pytest

import application_sdk.constants as constants
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    set_infrastructure,
)
from application_sdk.storage.factory import create_local_store
from application_sdk.storage.formats.utils import download_files
from application_sdk.storage.ops import (
    download_file,
    download_prefix,
    list_keys,
    upload_file,
    upload_file_from_bytes,
    upload_prefix,
)


@pytest.fixture
def store(tmp_path):
    """Create a real local object store backed by a temp directory."""
    return create_local_store(tmp_path / "store")


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Create a staging directory and set TEMPORARY_PATH to it."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setenv("ATLAN_TEMPORARY_PATH", str(staging_dir))
    monkeypatch.setattr(constants, "TEMPORARY_PATH", str(staging_dir))
    return staging_dir


# ------------------------------------------------------------------
# Upload + download roundtrip
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_download_roundtrip(store, tmp_path):
    """Upload a file, download it back, verify contents match."""
    src = tmp_path / "input.txt"
    src.write_text("hello integration test")

    sha = await upload_file("roundtrip/input.txt", src, store)
    assert len(sha) == 64

    dest = tmp_path / "output.txt"
    dl_sha = await download_file("roundtrip/input.txt", dest, store, compute_hash=True)
    assert dest.read_text() == "hello integration test"
    assert dl_sha == sha


# ------------------------------------------------------------------
# upload_file retain_local_copy
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_retain_local_copy_false_deletes_file(store, staging):
    """Upload with retain_local_copy=False should delete the local file."""
    src = staging / "ephemeral.txt"
    src.write_text("delete me after upload")

    await upload_file("retain/ephemeral.txt", src, store, retain_local_copy=False)
    assert not src.exists(), "File should be deleted after upload"

    # Verify the file was actually uploaded
    assert await list_keys("retain", store) == ["retain/ephemeral.txt"]


@pytest.mark.integration
async def test_upload_retain_local_copy_true_keeps_file(store, staging):
    """Upload with retain_local_copy=True (default) should keep the local file."""
    src = staging / "persistent.txt"
    src.write_text("keep me")

    await upload_file("retain/persistent.txt", src, store, retain_local_copy=True)
    assert src.exists(), "File should still exist after upload"


# ------------------------------------------------------------------
# download_prefix (concurrent)
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_download_prefix_concurrent_roundtrip(store, tmp_path):
    """Upload multiple files, download_prefix them concurrently, verify all."""
    file_count = 12
    for i in range(file_count):
        src = tmp_path / f"src_{i}.parquet"
        src.write_bytes(f"data-{i}".encode())
        await upload_file(f"batch/file_{i}.parquet", src, store)

    # Also upload a non-parquet file to test suffix filtering
    meta = tmp_path / "meta.json"
    meta.write_text("{}")
    await upload_file("batch/meta.json", meta, store)

    dest = tmp_path / "downloaded"
    paths = await download_prefix(
        "batch", dest, store, suffix=".parquet", max_concurrency=3
    )

    assert len(paths) == file_count
    for i in range(file_count):
        downloaded = dest / f"batch/file_{i}.parquet"
        assert downloaded.exists()
        assert downloaded.read_bytes() == f"data-{i}".encode()


@pytest.mark.integration
async def test_download_prefix_empty_prefix(store, tmp_path):
    """download_prefix with no matching keys returns empty list."""
    paths = await download_prefix("nonexistent", tmp_path, store)
    assert paths == []


# ------------------------------------------------------------------
# list_keys with suffix filter
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_list_keys_suffix_filter(store, tmp_path):
    """list_keys with suffix filters by file extension."""
    for name in ["a.parquet", "b.parquet", "c.json", "d.csv"]:
        src = tmp_path / name
        src.write_bytes(b"x")
        await upload_file(f"mixed/{name}", src, store)

    parquet = await list_keys("mixed", store, suffix=".parquet")
    assert len(parquet) == 2
    assert all(k.endswith(".parquet") for k in parquet)

    json_keys = await list_keys("mixed", store, suffix=".json")
    assert json_keys == ["mixed/c.json"]

    all_keys = await list_keys("mixed", store)
    assert len(all_keys) == 4


# ------------------------------------------------------------------
# download_files deprecation warning
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_download_files_emits_deprecation_warning(store, staging):
    """download_files() should emit a DeprecationWarning."""
    set_infrastructure(InfrastructureContext(storage=store))

    # Upload a file so download_files has something to find
    src = staging / "test.parquet"
    src.write_bytes(b"parquet-data")
    await upload_file("depwarn/test.parquet", src, store)

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            await download_files(str(staging / "depwarn/test.parquet"), ".parquet")
        except Exception:
            pass  # may fail on infra setup, we only care about the warning

        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "storage.transfer.download" in str(dep_warnings[0].message)


# ------------------------------------------------------------------
# upload_prefix (parallel directory upload)
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_prefix_roundtrip(store, tmp_path):
    """Upload a directory, list keys, download and verify."""
    src_dir = tmp_path / "upload_src"
    src_dir.mkdir()
    for i in range(5):
        (src_dir / f"file_{i}.txt").write_text(f"content-{i}")

    uploaded = await upload_prefix(local_dir=src_dir, prefix="batch/run1", store=store)
    assert len(uploaded) == 5

    keys = await list_keys("batch/run1", store)
    assert len(keys) == 5

    dest_dir = tmp_path / "download_dest"
    paths = await download_prefix("batch/run1", dest_dir, store)
    assert len(paths) == 5
    for i in range(5):
        downloaded = dest_dir / f"batch/run1/file_{i}.txt"
        assert downloaded.read_text() == f"content-{i}"


@pytest.mark.integration
async def test_upload_prefix_with_retain_local_false(store, staging):
    """Upload with retain_local_copy=False deletes source files."""
    src_dir = staging / "ephemeral_dir"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("aaa")
    (src_dir / "b.txt").write_text("bbb")

    await upload_prefix(
        local_dir=src_dir, prefix="ephemeral", store=store, retain_local_copy=False
    )

    # Local files should be deleted
    assert not (src_dir / "a.txt").exists()
    assert not (src_dir / "b.txt").exists()

    # But they should exist in the store
    keys = await list_keys("ephemeral", store)
    assert len(keys) == 2


# ------------------------------------------------------------------
# upload_file_from_bytes
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_file_from_bytes_roundtrip(store, tmp_path):
    """Upload bytes directly, then download and verify."""
    content = b"hello from bytes upload"
    sha = await upload_file_from_bytes("bytes/test.bin", content, store)
    assert len(sha) == 64  # hex SHA-256

    dest = tmp_path / "downloaded.bin"
    await download_file("bytes/test.bin", dest, store)
    assert dest.read_bytes() == content


@pytest.mark.integration
async def test_upload_file_from_bytes_empty(store, tmp_path):
    """Upload empty bytes."""
    sha = await upload_file_from_bytes("bytes/empty.bin", b"", store)
    assert len(sha) == 64

    dest = tmp_path / "empty.bin"
    await download_file("bytes/empty.bin", dest, store)
    assert dest.read_bytes() == b""
