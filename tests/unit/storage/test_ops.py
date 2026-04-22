"""Unit tests for storage ops using MemoryStore."""

from __future__ import annotations

import os
from unittest.mock import patch

import orjson
import pytest

import application_sdk.constants as constants
from application_sdk.storage.batch import download_prefix, list_keys
from application_sdk.storage.errors import StorageNotFoundError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import (
    _get_bytes,
    _put,
    delete,
    download_file,
    normalize_key,
    put_json,
    upload_file,
)


@pytest.fixture
def store():
    return create_memory_store()


class TestPutAndGet:
    async def test_put_then_get_returns_data(self, store) -> None:
        await _put("hello.txt", b"world", store)
        result = await _get_bytes("hello.txt", store)
        assert result == b"world"

    async def test_get_missing_key_returns_none(self, store) -> None:
        result = await _get_bytes("nonexistent/key.bin", store)
        assert result is None

    async def test_put_overwrites(self, store) -> None:
        await _put("key", b"v1", store)
        await _put("key", b"v2", store)
        assert await _get_bytes("key", store) == b"v2"

    async def test_put_empty_bytes(self, store) -> None:
        await _put("empty", b"", store)
        result = await _get_bytes("empty", store)
        assert result == b""


class TestDelete:
    async def test_delete_existing_key(self, store) -> None:
        await _put("del-me", b"data", store)
        deleted = await delete("del-me", store)
        assert deleted is True
        assert await _get_bytes("del-me", store) is None

    async def test_delete_missing_key_does_not_raise(self, store) -> None:
        # MemoryStore silently succeeds on delete of non-existent key
        result = await delete("not-there", store)
        assert isinstance(result, bool)


class TestListKeys:
    async def test_list_all_keys(self, store) -> None:
        await _put("a/b.txt", b"1", store)
        await _put("a/c.txt", b"2", store)
        keys = await list_keys(store=store)
        assert "a/b.txt" in keys
        assert "a/c.txt" in keys

    async def test_list_with_prefix(self, store) -> None:
        await _put("docs/x.txt", b"x", store)
        await _put("docs/y.txt", b"y", store)
        await _put("images/z.png", b"z", store)
        keys = await list_keys("docs/", store)
        assert "docs/x.txt" in keys
        assert "docs/y.txt" in keys
        assert "images/z.png" not in keys

    async def test_list_empty_store(self, store) -> None:
        keys = await list_keys(store=store)
        assert keys == []


class TestNormalizeKey:
    """Tests for normalize_key() — v2-compatible path normalisation."""

    def test_already_clean_key_is_unchanged(self) -> None:
        assert (
            normalize_key("artifacts/apps/foo/bar.jsonl")
            == "artifacts/apps/foo/bar.jsonl"
        )

    def test_leading_slash_is_stripped(self) -> None:
        assert normalize_key("/artifacts/foo.txt") == "artifacts/foo.txt"

    def test_trailing_slash_is_stripped(self) -> None:
        assert normalize_key("artifacts/foo/") == "artifacts/foo"

    def test_empty_string_returns_empty(self) -> None:
        assert normalize_key("") == ""

    def test_staging_path_strips_temporary_prefix(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        staging = os.path.join(TEMPORARY_PATH, "artifacts/apps/myapp/run-1/out.json")
        assert normalize_key(staging) == "artifacts/apps/myapp/run-1/out.json"

    def test_staging_root_returns_empty(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        assert normalize_key(TEMPORARY_PATH) == ""

    def test_absolute_path_strips_leading_slash(self) -> None:
        assert normalize_key("/data/output.parquet") == "data/output.parquet"

    def test_file_refs_key_is_unchanged(self) -> None:
        assert normalize_key("file_refs/abc123.jsonl") == "file_refs/abc123.jsonl"


class TestNormalizeIntegration:
    """normalize=True (default) round-trips staging paths transparently."""

    async def test_put_staging_path_readable_as_store_key(self, store) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        staging = os.path.join(TEMPORARY_PATH, "artifacts/apps/app/wf/run/data.bin")
        await _put(staging, b"payload", store)
        # Can be retrieved with the normalised key
        result = await _get_bytes("artifacts/apps/app/wf/run/data.bin", store)
        assert result == b"payload"

    async def test_put_and_get_with_normalize_false_uses_exact_key(self, store) -> None:
        await _put("exact/key.bin", b"data", store, normalize=False)
        result = await _get_bytes("exact/key.bin", store, normalize=False)
        assert result == b"data"

    async def test_list_keys_adds_trailing_slash_to_prefix(self, store) -> None:
        await _put("docs/a.txt", b"a", store, normalize=False)
        await _put("docs_extra/b.txt", b"b", store, normalize=False)
        # "docs" without trailing slash should NOT match "docs_extra/"
        keys = await list_keys("docs", store, normalize=True)
        assert "docs/a.txt" in keys
        assert "docs_extra/b.txt" not in keys

    async def test_list_keys_suffix_filter(self, store) -> None:
        await _put("data/table.parquet", b"p", store, normalize=False)
        await _put("data/table.json", b"j", store, normalize=False)
        await _put("data/stats.parquet", b"p2", store, normalize=False)

        parquet_keys = await list_keys("data", store, suffix=".parquet")
        assert len(parquet_keys) == 2
        assert "data/table.parquet" in parquet_keys
        assert "data/stats.parquet" in parquet_keys
        assert "data/table.json" not in parquet_keys

        json_keys = await list_keys("data", store, suffix=".json")
        assert json_keys == ["data/table.json"]

        all_keys = await list_keys("data", store)
        assert len(all_keys) == 3


class TestUploadFile:
    async def test_upload_file_roundtrip(self, store, tmp_path) -> None:
        f = tmp_path / "data.bin"
        content = b"hello streaming world"
        f.write_bytes(content)

        sha256 = await upload_file("test/data.bin", f, store)
        assert len(sha256) == 64  # hex SHA-256

        # Verify what was stored
        dest = tmp_path / "out.bin"
        dl_sha256 = await download_file("test/data.bin", dest, store, compute_hash=True)
        assert dest.read_bytes() == content
        assert dl_sha256 == sha256

    async def test_upload_file_returns_correct_sha256(self, store, tmp_path) -> None:
        import hashlib

        content = b"checksum me"
        f = tmp_path / "check.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()

        sha256 = await upload_file("check.bin", f, store)
        assert sha256 == expected

    async def test_upload_file_normalize_false(self, store, tmp_path) -> None:
        f = tmp_path / "x.bin"
        f.write_bytes(b"exact")
        await upload_file("exact/key.bin", f, store, normalize=False)
        raw = await _get_bytes("exact/key.bin", store, normalize=False)
        assert raw == b"exact"

    async def test_upload_file_retain_local_copy_true(self, store, tmp_path) -> None:
        f = tmp_path / "keep.bin"
        f.write_bytes(b"keep me")
        await upload_file("keep.bin", f, store, retain_local_copy=True)
        assert f.exists(), "Local file should be retained"

    async def test_upload_file_retain_local_copy_false_in_staging(
        self, store, tmp_path
    ) -> None:
        # Simulate file inside TEMPORARY_PATH so it's allowed to be deleted
        staging = tmp_path / "staging"
        staging.mkdir()
        f = staging / "delete_me.bin"
        f.write_bytes(b"delete me")

        with patch.object(constants, "TEMPORARY_PATH", str(staging)):
            await upload_file("del.bin", f, store, retain_local_copy=False)
        assert not f.exists(), "Local file should be deleted after upload"

    async def test_upload_file_retain_local_copy_false_outside_staging(
        self, store, tmp_path
    ) -> None:
        # File outside TEMPORARY_PATH should NOT be deleted (path traversal protection)
        f = tmp_path / "safe.bin"
        f.write_bytes(b"safe")

        with patch.object(constants, "TEMPORARY_PATH", str(tmp_path / "other")):
            await upload_file("safe.bin", f, store, retain_local_copy=False)
        assert f.exists(), "File outside staging should NOT be deleted"


class TestDownloadPrefix:
    async def test_download_prefix_basic(self, store, tmp_path) -> None:
        await _put("myprefix/a.txt", b"aaa", store, normalize=False)
        await _put("myprefix/b.txt", b"bbb", store, normalize=False)
        await _put("other/c.txt", b"ccc", store, normalize=False)

        paths = await download_prefix("myprefix", tmp_path, store=store)
        assert len(paths) == 2
        assert (tmp_path / "myprefix" / "a.txt").read_bytes() == b"aaa"
        assert (tmp_path / "myprefix" / "b.txt").read_bytes() == b"bbb"

    async def test_download_prefix_with_suffix_filter(self, store, tmp_path) -> None:
        await _put("data/file.parquet", b"pq", store, normalize=False)
        await _put("data/file.json", b"js", store, normalize=False)

        paths = await download_prefix("data", tmp_path, store=store, suffix=".parquet")
        assert len(paths) == 1
        assert paths[0].endswith("file.parquet")

    async def test_download_prefix_concurrent(self, store, tmp_path) -> None:
        """Verify multiple files are downloaded concurrently."""
        for i in range(8):
            await _put(
                f"batch/file_{i}.txt", f"content_{i}".encode(), store, normalize=False
            )

        paths = await download_prefix("batch", tmp_path, store=store, max_concurrency=3)
        assert len(paths) == 8
        for i in range(8):
            assert (
                tmp_path / f"batch/file_{i}.txt"
            ).read_bytes() == f"content_{i}".encode()

    async def test_download_prefix_empty(self, store, tmp_path) -> None:
        """Empty prefix returns no files."""
        paths = await download_prefix("nonexistent", tmp_path, store=store)
        assert paths == []

    async def test_download_prefix_respects_max_concurrency(
        self, store, tmp_path
    ) -> None:
        """Verify semaphore limits concurrent downloads."""
        from unittest.mock import patch

        for i in range(6):
            await _put(f"sem/f_{i}.txt", b"x", store, normalize=False)

        max_active = 0
        active = 0
        original_download = download_file

        async def tracking_download(*args, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                return await original_download(*args, **kwargs)
            finally:
                active -= 1

        with patch(
            "application_sdk.storage.ops.download_file", side_effect=tracking_download
        ):
            await download_prefix("sem", tmp_path, store=store, max_concurrency=2)

        assert max_active <= 2, f"Expected max 2 concurrent, got {max_active}"


class TestDownloadFile:
    async def test_download_file_missing_key_raises(self, store, tmp_path) -> None:
        with pytest.raises(StorageNotFoundError):
            await download_file("no/such/key.bin", tmp_path / "out.bin", store)

    async def test_download_file_no_hash(self, store, tmp_path) -> None:
        f = tmp_path / "src.bin"
        f.write_bytes(b"payload")
        await upload_file("payload.bin", f, store)

        dest = tmp_path / "dest.bin"
        result = await download_file("payload.bin", dest, store, compute_hash=False)
        assert result is None
        assert dest.read_bytes() == b"payload"

    async def test_download_file_creates_parent_dirs(self, store, tmp_path) -> None:
        f = tmp_path / "src.bin"
        f.write_bytes(b"nested")
        await upload_file("n.bin", f, store)

        dest = tmp_path / "a" / "b" / "c" / "out.bin"
        await download_file("n.bin", dest, store)
        assert dest.read_bytes() == b"nested"


class TestPutJson:
    """Tests for the public put_json() helper."""

    async def test_serialises_dict_and_writes(self, store) -> None:
        payload = {"workflow_id": "wf-1", "count": 42}
        await put_json("configs/wf-1.json", payload, store)
        raw = await _get_bytes("configs/wf-1.json", store)
        assert raw == orjson.dumps(payload)

    async def test_normalises_staging_path_by_default(self, store) -> None:
        staging = os.path.join(constants.TEMPORARY_PATH, "configs/wf-2.json")
        await put_json(staging, {"x": 1}, store)
        raw = await _get_bytes("configs/wf-2.json", store)
        assert raw == orjson.dumps({"x": 1})

    async def test_normalize_false_uses_exact_key(self, store) -> None:
        await put_json("exact/key.json", [1, 2, 3], store, normalize=False)
        raw = await _get_bytes("exact/key.json", store, normalize=False)
        assert raw == orjson.dumps([1, 2, 3])

    async def test_accepts_list_and_primitives(self, store) -> None:
        for value, key in [
            ([1, 2], "list.json"),
            ("hello", "str.json"),
            (99, "int.json"),
            (True, "bool.json"),
            (None, "null.json"),
        ]:
            await put_json(key, value, store)
            raw = await _get_bytes(key, store)
            assert raw == orjson.dumps(value), f"serialisation mismatch for key={key!r}"
