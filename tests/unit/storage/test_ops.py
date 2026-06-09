"""Unit tests for storage ops using MemoryStore."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest

from application_sdk import constants
from application_sdk.storage.batch import download_prefix, list_keys
from application_sdk.storage.errors import StorageError, StorageNotFoundError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import (
    _get_bytes,
    _put,
    delete,
    download_file,
    download_file_chunked,
    exists,
    get_file_size,
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

    async def test_upload_file_compute_hash_false_returns_none(
        self, store, tmp_path
    ) -> None:
        """``compute_hash=False`` skips the digest and returns None.

        Regression guard: PR #1624 introduced ``compute_hash`` so external
        stores (CloudStore) can avoid the integrity sidecar.  Removing the
        parameter would silently start computing hashes for every external
        upload — this test pins the contract.
        """
        f = tmp_path / "no_hash.bin"
        f.write_bytes(b"do not hash me")

        result = await upload_file("no_hash.bin", f, store, compute_hash=False)
        assert result is None

    async def test_upload_file_compute_hash_default_returns_digest(
        self, store, tmp_path
    ) -> None:
        """The default ``compute_hash=True`` continues to return a hex digest."""
        import hashlib

        content = b"default behaviour"
        f = tmp_path / "default.bin"
        f.write_bytes(content)

        result = await upload_file("default.bin", f, store)
        assert result == hashlib.sha256(content).hexdigest()

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


# ---------------------------------------------------------------------------
# Typed not-found detection (BLDX-1155 #5: typed obstore exceptions)
# ---------------------------------------------------------------------------


class TestIsNotFound:
    """The not-found helper must recognise both built-in and typed exceptions."""

    def test_recognises_filenotfounderror(self) -> None:
        from application_sdk.storage.ops import _is_not_found

        assert _is_not_found(FileNotFoundError("/no/such")) is True

    def test_recognises_obstore_typed_notfounderror(self) -> None:
        """obstore.exceptions.NotFoundError must be classified as not-found."""
        from application_sdk.storage.ops import _is_not_found

        try:
            from obstore.exceptions import NotFoundError as ObstoreNotFoundError
        except ImportError:  # pragma: no cover — older obstore
            pytest.skip("obstore.exceptions.NotFoundError not available")

        assert _is_not_found(ObstoreNotFoundError("missing key")) is True

    def test_does_not_match_unrelated_messages(self) -> None:
        from application_sdk.storage.ops import _is_not_found

        assert _is_not_found(RuntimeError("internal failure")) is False
        assert _is_not_found(PermissionError("denied")) is False

    def test_substring_fallback_still_works(self) -> None:
        """Generic obstore errors carrying 404 strings remain identifiable."""
        from application_sdk.storage.ops import _is_not_found

        assert _is_not_found(RuntimeError("got HTTP 404 from S3")) is True
        assert _is_not_found(RuntimeError("key not found in bucket")) is True


# ---------------------------------------------------------------------------
# Structured transfer logs (BLDX-1155 #6: surface what's actually happening)
# ---------------------------------------------------------------------------


class TestTransferLogging:
    """upload_file / download_file must emit structured per-attempt log events.

    A prior RCA was wrong-footed by the absence of these fields: we said
    "single attempt" when ~5–6 attempts had actually happened in the Rust
    layer. Even though we cannot count Rust retries directly, we *can*
    expose what the SDK observed: bytes, elapsed, throughput, outcome, error
    class. That alone closes the worst gap.
    """

    async def test_upload_emits_success_log_with_metrics(
        self, store, tmp_path, loguru_capture
    ) -> None:
        f = tmp_path / "p.bin"
        f.write_bytes(b"x" * (256 * 1024))
        await upload_file("metrics/up.bin", f, store)

        events = [r for r in loguru_capture if r["extra"].get("storage_op")]
        outcome_events = [r for r in events if r["extra"].get("outcome") == "success"]
        assert outcome_events, (
            "expected at least one structured 'success' upload event; "
            f"got events: {[r['message'] for r in events]}"
        )
        evt = outcome_events[-1]
        assert evt["extra"].get("storage_op") == "upload"
        assert evt["extra"].get("size_bytes") == 256 * 1024
        assert evt["extra"].get("elapsed_ms") is not None
        assert evt["extra"].get("store_path") == "metrics/up.bin"

    async def test_download_emits_failure_log_with_error_class(
        self, store, tmp_path, loguru_capture
    ) -> None:
        with pytest.raises(StorageNotFoundError):
            await download_file("no/such/key.bin", tmp_path / "out.bin", store)

        events = [r for r in loguru_capture if r["extra"].get("storage_op")]
        failure_events = [r for r in events if r["extra"].get("outcome") == "failure"]
        assert failure_events, (
            "expected at least one structured 'failure' download event; "
            f"got events: {[r['message'] for r in events]}"
        )
        evt = failure_events[-1]
        assert evt["extra"].get("storage_op") == "download"
        assert evt["extra"].get("error_class") is not None
        # Not-found should be classified explicitly.
        assert evt["extra"].get("error_class") in {
            "StorageNotFoundError",
            "FileNotFoundError",
        } or "NotFound" in evt["extra"].get("error_class", "")


# ---------------------------------------------------------------------------
# delete / exists / upload / download — error handling and behaviour
# ---------------------------------------------------------------------------


async def test_delete_wraps_non_404_failure() -> None:
    store = MagicMock()
    with (
        patch(
            "application_sdk.storage.ops.obstore.delete_async",
            new=AsyncMock(side_effect=RuntimeError("permission denied")),
        ),
        pytest.raises(StorageError),
    ):
        await delete("k", store, normalize=False)


async def test_delete_returns_false_on_404() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.delete_async",
        new=AsyncMock(side_effect=RuntimeError("404")),
    ):
        assert await delete("k", store, normalize=False) is False


async def test_exists_returns_false_on_404() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.head_async",
        new=AsyncMock(side_effect=RuntimeError("not found")),
    ):
        assert await exists("k", store, normalize=False) is False


async def test_exists_wraps_non_404_failure() -> None:
    store = MagicMock()
    with (
        patch(
            "application_sdk.storage.ops.obstore.head_async",
            new=AsyncMock(side_effect=RuntimeError("server error")),
        ),
        pytest.raises(StorageError),
    ):
        await exists("k", store, normalize=False)


async def test_exists_returns_true_on_success() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.head_async",
        new=AsyncMock(return_value=MagicMock()),
    ):
        assert await exists("k", store, normalize=False) is True


async def test_upload_file_wraps_failure_as_storage_error(tmp_path) -> None:
    """The writer raises mid-upload; ops must wrap it as StorageError."""
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")

    class _BoomCM:
        async def __aenter__(self) -> None:
            raise RuntimeError("network")

        async def __aexit__(self, *exc) -> None:  # pragma: no cover - not reached
            return None

    with (
        patch(
            "application_sdk.storage.ops.obstore.open_writer_async",
            return_value=_BoomCM(),
        ),
        pytest.raises(StorageError) as excinfo,
    ):
        await upload_file("k", f, MagicMock(), normalize=False)
    assert excinfo.value.key == "k"


async def test_download_file_translates_404_to_not_found(tmp_path) -> None:
    """Download must translate a 404 from obstore into StorageNotFoundError."""
    with (
        patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(side_effect=RuntimeError("Key not found")),
        ),
        pytest.raises(StorageNotFoundError),
    ):
        await download_file("k", tmp_path / "out.bin", MagicMock(), normalize=False)


async def test_download_file_wraps_non_404_get_failure(tmp_path) -> None:
    with (
        patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(side_effect=RuntimeError("permission denied")),
        ),
        pytest.raises(StorageError) as excinfo,
    ):
        await download_file("k", tmp_path / "out.bin", MagicMock(), normalize=False)
    assert not isinstance(excinfo.value, StorageNotFoundError)


# ---------------------------------------------------------------------------
# get_file_size (BLDX-1155: new public function)
# ---------------------------------------------------------------------------


class TestGetFileSize:
    async def test_returns_size_for_existing_key(self, store, tmp_path) -> None:
        content = b"hello world"
        await _put("size/file.bin", content, store, normalize=False)
        size = await get_file_size("size/file.bin", store, normalize=False)
        assert size == len(content)

    async def test_returns_none_for_missing_key(self, store) -> None:
        size = await get_file_size("no/such/key.bin", store, normalize=False)
        assert size is None

    async def test_non_404_error_raises_storage_error(self) -> None:
        store = MagicMock()
        with (
            patch(
                "application_sdk.storage.ops.obstore.head_async",
                new=AsyncMock(side_effect=RuntimeError("permission denied")),
            ),
            pytest.raises(StorageError),
        ):
            await get_file_size("k", store, normalize=False)


# ---------------------------------------------------------------------------
# download_file_chunked (BLDX-1155: new public function)
# ---------------------------------------------------------------------------


class TestDownloadFileChunked:
    async def test_small_file_falls_through_to_download_file(
        self, store, tmp_path
    ) -> None:
        """File <= chunk_size_bytes delegates to the single-stream download_file."""
        content = b"small"
        await _put("chunked/small.bin", content, store, normalize=False)
        dest = tmp_path / "out.bin"

        import application_sdk.storage.ops as ops_mod

        with patch.object(
            ops_mod, "download_file", wraps=ops_mod.download_file
        ) as mock_dl:
            result = await download_file_chunked(
                "chunked/small.bin",
                dest,
                store,
                chunk_size_bytes=1024,
                normalize=False,
            )

        mock_dl.assert_awaited_once()
        assert dest.read_bytes() == content
        assert result is not None
        assert len(result) == 64

    async def test_multi_chunk_produces_correct_content(self, store, tmp_path) -> None:
        """File split into multiple chunks is reassembled byte-for-byte correctly."""
        import hashlib

        content = b"ABCDEFGHIJ"  # 10 bytes → 4 chunks at chunk_size=3 (3,3,3,1)
        await _put("chunked/multi.bin", content, store, normalize=False)
        dest = tmp_path / "out.bin"

        sha = await download_file_chunked(
            "chunked/multi.bin",
            dest,
            store,
            chunk_size_bytes=3,
            max_concurrent_chunks=2,
            normalize=False,
        )

        assert dest.read_bytes() == content
        assert sha == hashlib.sha256(content).hexdigest()

    async def test_not_found_raises_storage_not_found_error(
        self, store, tmp_path
    ) -> None:
        with pytest.raises(StorageNotFoundError):
            await download_file_chunked(
                "no/such/key.bin",
                tmp_path / "out.bin",
                store,
                normalize=False,
            )

    async def test_compute_hash_false_returns_none(self, store, tmp_path) -> None:
        content = b"no hash needed"
        await _put("chunked/nohash.bin", content, store, normalize=False)
        dest = tmp_path / "out.bin"

        result = await download_file_chunked(
            "chunked/nohash.bin",
            dest,
            store,
            chunk_size_bytes=3,
            compute_hash=False,
            normalize=False,
        )

        assert result is None
        assert dest.read_bytes() == content

    async def test_compute_hash_true_returns_sha256(self, store, tmp_path) -> None:
        import hashlib

        content = b"hash me chunked"
        await _put("chunked/hash.bin", content, store, normalize=False)
        dest = tmp_path / "out.bin"

        result = await download_file_chunked(
            "chunked/hash.bin",
            dest,
            store,
            chunk_size_bytes=3,
            compute_hash=True,
            normalize=False,
        )

        assert result == hashlib.sha256(content).hexdigest()

    async def test_partial_file_cleaned_up_on_error(self, store, tmp_path) -> None:
        """If a chunk fails mid-download, the partial output file is deleted."""
        content = b"ABCDEFGHIJ"
        await _put("chunked/boom.bin", content, store, normalize=False)
        dest = tmp_path / "out.bin"

        import application_sdk.storage.ops as ops_mod

        real_get_range = ops_mod.obstore.get_range_async
        call_count = 0

        async def flaky_get_range(st, key, *, start, length):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("transient chunk failure")
            return await real_get_range(st, key, start=start, length=length)

        with (
            patch.object(
                ops_mod.obstore, "get_range_async", side_effect=flaky_get_range
            ),
            pytest.raises((StorageError, RuntimeError)),
        ):
            await download_file_chunked(
                "chunked/boom.bin",
                dest,
                store,
                chunk_size_bytes=3,
                normalize=False,
            )

        assert not dest.exists(), "partial file must be cleaned up after chunk failure"


async def test_download_file_wraps_stream_failure(tmp_path) -> None:
    """If streaming raises mid-download, the failure must be wrapped."""

    class _BoomStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("disk full")

    result_obj = MagicMock()
    result_obj.stream.return_value = _BoomStream()

    with (
        patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(return_value=result_obj),
        ),
        pytest.raises(StorageError),
    ):
        await download_file("k", tmp_path / "out.bin", MagicMock(), normalize=False)


async def test_upload_file_does_not_delete_when_retain_local_copy_true(
    tmp_path,
) -> None:
    """Retain flag honoured even when path is inside staging."""
    f = tmp_path / "kept.bin"
    f.write_bytes(b"data")

    class _DummyCM:
        async def __aenter__(self):
            self.writer = AsyncMock()
            self.writer.write = AsyncMock()
            return self.writer

        async def __aexit__(self, *exc):
            return None

    with patch(
        "application_sdk.storage.ops.obstore.open_writer_async",
        return_value=_DummyCM(),
    ):
        digest = await upload_file(
            "k", f, MagicMock(), retain_local_copy=True, normalize=False
        )
    assert isinstance(digest, str)
    assert f.exists()


# ---------------------------------------------------------------------------
# _safe_join_under — containment guard for prefix downloads (issue #1694)
# ---------------------------------------------------------------------------


class TestSafeJoinUnder:
    def test_simple_key_resolves_inside_root(self, tmp_path) -> None:
        from application_sdk.storage.ops import _safe_join_under

        result = _safe_join_under(tmp_path, "sub/file.txt")
        assert result == (tmp_path / "sub" / "file.txt").resolve()
        assert result.is_relative_to(tmp_path.resolve())

    def test_leading_slash_stripped(self, tmp_path) -> None:
        from application_sdk.storage.ops import _safe_join_under

        result = _safe_join_under(tmp_path, "/a/b.txt")
        assert result == (tmp_path / "a" / "b.txt").resolve()

    def test_parent_traversal_rejected(self, tmp_path) -> None:
        from application_sdk.storage.ops import _safe_join_under

        with pytest.raises(StorageError, match="Path traversal"):
            _safe_join_under(tmp_path, "../escape.txt")

    def test_mixed_traversal_rejected(self, tmp_path) -> None:
        from application_sdk.storage.ops import _safe_join_under

        with pytest.raises(StorageError, match="Path traversal"):
            _safe_join_under(tmp_path, "safe/../../escape.txt")

    def test_empty_rel_returns_root(self, tmp_path) -> None:
        from application_sdk.storage.ops import _safe_join_under

        result = _safe_join_under(tmp_path, "")
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _resolve_put_attributes — identity-based infra-store matching
# ---------------------------------------------------------------------------


class TestResolvePutAttributes:
    """_resolve_put_attributes returns the correct put attributes for every store variant.

    Covers:
    - BoundStore: returns embedded put_attributes directly (no infra lookup needed).
    - store=None: returns infra.storage_put_attributes.
    - store IS infra.storage: identity match, returns infra.storage_put_attributes.
    - store IS infra.upstream_storage: returns infra.upstream_storage_put_attributes.
    - unrelated store: returns None.
    - no infra context: returns None.
    """

    @pytest.fixture(autouse=True)
    def _clear_infra(self):
        from application_sdk.infrastructure.context import clear_infrastructure

        clear_infrastructure()
        yield
        clear_infrastructure()

    def _make_infra(
        self, store, put_attrs, *, upstream_store=None, upstream_put_attrs=None
    ):
        from application_sdk.infrastructure.context import InfrastructureContext

        return InfrastructureContext(
            storage=store,
            storage_put_attributes=put_attrs,
            upstream_storage=upstream_store,
            upstream_storage_put_attributes=upstream_put_attrs,
        )

    def test_none_store_returns_infra_put_attributes(self) -> None:
        from application_sdk.infrastructure.context import set_infrastructure
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _resolve_put_attributes

        store = create_memory_store()
        put_attrs = {"Storage-Class": "STANDARD_IA"}
        set_infrastructure(self._make_infra(store, put_attrs))
        assert _resolve_put_attributes(None) == put_attrs

    def test_explicit_infra_store_returns_put_attributes(self) -> None:
        """Identity match: explicit store IS infra.storage → attributes returned."""
        from application_sdk.infrastructure.context import set_infrastructure
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _resolve_put_attributes

        store = create_memory_store()
        put_attrs = {"Storage-Class": "STANDARD_IA"}
        set_infrastructure(self._make_infra(store, put_attrs))
        assert _resolve_put_attributes(store) == put_attrs

    def test_upstream_store_returns_upstream_put_attributes(self) -> None:
        """SDR mode: store IS infra.upstream_storage → upstream put_attributes returned."""
        from application_sdk.infrastructure.context import set_infrastructure
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _resolve_put_attributes

        deploy_store = create_memory_store()
        upstream_store = create_memory_store()
        upstream_put_attrs = {"Storage-Class": "INTELLIGENT_TIERING"}
        set_infrastructure(
            self._make_infra(
                deploy_store,
                {"Storage-Class": "STANDARD_IA"},
                upstream_store=upstream_store,
                upstream_put_attrs=upstream_put_attrs,
            )
        )
        assert _resolve_put_attributes(upstream_store) == upstream_put_attrs

    def test_bound_store_returns_embedded_put_attributes(self) -> None:
        """BoundStore: put_attributes read from the wrapper, no infra context needed."""
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import BoundStore, _resolve_put_attributes

        raw = create_memory_store()
        put_attrs = {"Storage-Class": "GLACIER"}
        bound = BoundStore(raw, put_attrs)
        assert _resolve_put_attributes(bound) == put_attrs

    def test_bound_store_none_put_attributes(self) -> None:
        """BoundStore with no put_attributes returns None."""
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import BoundStore, _resolve_put_attributes

        bound = BoundStore(create_memory_store(), None)
        assert _resolve_put_attributes(bound) is None

    def test_resolve_store_unwraps_bound_store(self) -> None:
        """_resolve_store returns the inner ObjectStore when given a BoundStore."""
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import BoundStore, _resolve_store

        raw = create_memory_store()
        bound = BoundStore(raw, {"Storage-Class": "GLACIER"})
        assert _resolve_store(bound) is raw

    def test_unrelated_store_returns_none(self) -> None:
        """A different store (e.g. CloudStore, test store) must not get infra attrs."""
        from application_sdk.infrastructure.context import set_infrastructure
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _resolve_put_attributes

        infra_store = create_memory_store()
        other_store = create_memory_store()
        set_infrastructure(
            self._make_infra(infra_store, {"Storage-Class": "STANDARD_IA"})
        )
        assert _resolve_put_attributes(other_store) is None

    def test_no_infra_context_returns_none(self) -> None:
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _resolve_put_attributes

        assert _resolve_put_attributes(None) is None
        assert _resolve_put_attributes(create_memory_store()) is None


# Azure container-not-found detection
# ---------------------------------------------------------------------------


class TestIsAzureContainerNotFound:
    """_is_azure_container_not_found must recognise container-absent errors."""

    def test_container_not_found_code(self) -> None:
        from application_sdk.storage.ops import _is_azure_container_not_found

        assert (
            _is_azure_container_not_found(
                RuntimeError(
                    "AzureError: ContainerNotFound — "
                    "The specified container does not exist."
                )
            )
            is True
        )

    def test_prose_message(self) -> None:
        from application_sdk.storage.ops import _is_azure_container_not_found

        assert (
            _is_azure_container_not_found(
                RuntimeError("The specified container does not exist.")
            )
            is True
        )

    def test_unrelated_error_returns_false(self) -> None:
        from application_sdk.storage.ops import _is_azure_container_not_found

        assert _is_azure_container_not_found(RuntimeError("BlobNotFound")) is False
        assert _is_azure_container_not_found(RuntimeError("permission denied")) is False
        assert _is_azure_container_not_found(FileNotFoundError("key")) is False

    async def test_put_raises_storage_config_error_on_container_not_found(
        self, tmp_path
    ) -> None:
        """_put must re-raise as StorageConfigError for Azure container-absent."""
        from application_sdk.storage.errors import StorageConfigError
        from application_sdk.storage.factory import create_memory_store

        azure_err = RuntimeError(
            "AzureError: ContainerNotFound — " "The specified container does not exist."
        )
        store = create_memory_store()
        with patch("obstore.put_async", AsyncMock(side_effect=azure_err)):
            with pytest.raises(StorageConfigError, match="pre-create the container"):
                await _put("key/data.json", b"{}", store=store)

    async def test_upload_file_raises_storage_config_error_on_container_not_found(
        self, tmp_path
    ) -> None:
        """upload_file must re-raise as StorageConfigError for Azure container-absent."""
        from application_sdk.storage.errors import StorageConfigError
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import upload_file

        src = tmp_path / "data.json"
        src.write_bytes(b"{}")
        azure_err = RuntimeError(
            "AzureError: ContainerNotFound — " "The specified container does not exist."
        )
        store = create_memory_store()
        with (
            patch(
                "application_sdk.storage.ops.obstore.open_writer_async",
                side_effect=azure_err,
            ),
            pytest.raises(StorageConfigError, match="pre-create the container"),
        ):
            await upload_file("key/data.json", src, store=store)
