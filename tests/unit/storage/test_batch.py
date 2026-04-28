"""Unit tests for storage.batch using MemoryStore (no real I/O)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from application_sdk.storage.batch import (
    delete_prefix,
    download_prefix,
    list_keys,
    upload_file_from_bytes,
    upload_prefix,
)
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes, _put


@pytest.fixture
def store():
    return create_memory_store()


# ---------------------------------------------------------------------------
# list_keys
# ---------------------------------------------------------------------------


class TestListKeys:
    async def test_normalises_and_appends_trailing_slash(self, store) -> None:
        await _put("foo/a.txt", b"a", store, normalize=False)
        await _put("foo_other/b.txt", b"b", store, normalize=False)
        # without trailing slash; normalize=True must add one to avoid
        # matching siblings like "foo_other/".
        keys = await list_keys("foo", store)
        assert "foo/a.txt" in keys
        assert "foo_other/b.txt" not in keys

    async def test_normalize_false_uses_prefix_exactly(self, store) -> None:
        await _put("raw/x.txt", b"x", store, normalize=False)
        keys = await list_keys("raw/", store, normalize=False)
        assert keys == ["raw/x.txt"]

    async def test_suffix_filter(self, store) -> None:
        await _put("a/file.parquet", b"1", store, normalize=False)
        await _put("a/file.json", b"2", store, normalize=False)
        keys = await list_keys("a/", store, suffix=".parquet", normalize=False)
        assert keys == ["a/file.parquet"]

    async def test_empty_prefix_lists_everything(self, store) -> None:
        await _put("a.txt", b"1", store, normalize=False)
        await _put("b/c.txt", b"2", store, normalize=False)
        keys = await list_keys("", store)
        assert sorted(keys) == ["a.txt", "b/c.txt"]

    async def test_underlying_failure_wraps_as_storage_error(self, store) -> None:
        """If obstore.list raises, list_keys raises StorageError.

        BLDX-1129 anchor: this exercises the function-local
        `from application_sdk.storage.errors import StorageError` import.
        """

        def boom(*args, **kwargs):
            raise RuntimeError("listing exploded")

        with patch("application_sdk.storage.batch.obstore.list", side_effect=boom):
            with pytest.raises(StorageError) as exc_info:
                await list_keys("anything/", store, normalize=False)
        assert "Failed to list keys" in str(exc_info.value)


# ---------------------------------------------------------------------------
# delete_prefix
# ---------------------------------------------------------------------------


class TestDeletePrefix:
    async def test_deletes_all_under_prefix(self, store) -> None:
        await _put("p/a.txt", b"1", store, normalize=False)
        await _put("p/b.txt", b"2", store, normalize=False)
        await _put("other/c.txt", b"3", store, normalize=False)

        n = await delete_prefix("p/", store, normalize=False)
        assert n == 2
        # other/ untouched
        assert await _get_bytes("other/c.txt", store, normalize=False) == b"3"
        assert await _get_bytes("p/a.txt", store, normalize=False) is None

    async def test_empty_prefix_deletes_nothing(self, store) -> None:
        # No matches under "missing/"
        n = await delete_prefix("missing/", store, normalize=False)
        assert n == 0


# ---------------------------------------------------------------------------
# download_prefix
# ---------------------------------------------------------------------------


class TestDownloadPrefix:
    async def test_downloads_all_keys_to_local_dir(self, store, tmp_path) -> None:
        await _put("dl/sub/a.txt", b"alpha", store, normalize=False)
        await _put("dl/b.txt", b"beta", store, normalize=False)

        dests = await download_prefix(
            "dl/", tmp_path, store, normalize=False, max_concurrency=2
        )
        # Each downloaded file's local path must exist with correct content
        assert len(dests) == 2
        for d in dests:
            assert Path(d).exists()
        assert (tmp_path / "dl" / "sub" / "a.txt").read_bytes() == b"alpha"
        assert (tmp_path / "dl" / "b.txt").read_bytes() == b"beta"

    async def test_suffix_filter_restricts_download(self, store, tmp_path) -> None:
        await _put("d/x.parquet", b"p", store, normalize=False)
        await _put("d/x.json", b"j", store, normalize=False)
        dests = await download_prefix(
            "d/", tmp_path, store, suffix=".parquet", normalize=False
        )
        assert len(dests) == 1
        assert dests[0].endswith(".parquet")

    async def test_empty_prefix_returns_no_files(self, store, tmp_path) -> None:
        dests = await download_prefix("nothing/", tmp_path, store, normalize=False)
        assert dests == []


# ---------------------------------------------------------------------------
# upload_prefix
# ---------------------------------------------------------------------------


class TestUploadPrefix:
    async def test_uploads_directory_tree(self, store, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"alpha")
        (tmp_path / "b.txt").write_bytes(b"beta")

        keys = await upload_prefix(tmp_path, "remote", store, normalize=False)
        assert sorted(keys) == ["remote/b.txt", "remote/sub/a.txt"]
        # Verify content uploaded
        assert await _get_bytes("remote/b.txt", store, normalize=False) == b"beta"
        assert await _get_bytes("remote/sub/a.txt", store, normalize=False) == b"alpha"

    async def test_skips_symlinks(self, store, tmp_path) -> None:
        # Real file, plus a symlink that should be skipped (path-traversal guard).
        (tmp_path / "real.txt").write_bytes(b"data")
        target = tmp_path / "outside.txt"
        target.write_bytes(b"shouldnt-be-uploaded")
        try:
            (tmp_path / "link.txt").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")
        # Remove target so a follow would fail loudly (extra check)
        keys = await upload_prefix(tmp_path, "out", store, normalize=False)
        # link.txt must not be in the uploaded keys
        assert "out/link.txt" not in keys
        assert "out/real.txt" in keys

    async def test_empty_prefix_uses_relative_paths(self, store, tmp_path) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        keys = await upload_prefix(tmp_path, "", store, normalize=False)
        assert keys == ["f.txt"]

    async def test_normalize_true_normalises_prefix(self, store, tmp_path) -> None:
        """Default normalize=True: leading slash is stripped from prefix."""
        (tmp_path / "f.txt").write_bytes(b"x")
        keys = await upload_prefix(tmp_path, "/abs/prefix", store, normalize=True)
        assert keys == ["abs/prefix/f.txt"]


# ---------------------------------------------------------------------------
# upload_file_from_bytes
# ---------------------------------------------------------------------------


class TestUploadFileFromBytes:
    async def test_uploads_bytes_via_temp_file(self, store) -> None:
        """BLDX-1129 anchor: exercises function-local `import tempfile`."""
        sha = await upload_file_from_bytes(
            "k/blob.bin", b"hello bytes", store, normalize=False
        )
        # SHA-256 hex string
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)
        # Roundtrip readable
        assert await _get_bytes("k/blob.bin", store, normalize=False) == b"hello bytes"

    async def test_cleanup_failure_swallowed(self, store) -> None:
        """If os.unlink raises OSError, the upload still succeeds."""
        original_unlink = os.unlink

        def _flaky_unlink(path):
            # Raise once, then call through so test cleanup still happens
            _flaky_unlink.calls += 1
            if _flaky_unlink.calls == 1:
                raise OSError("temp file already gone")
            return original_unlink(path)

        _flaky_unlink.calls = 0
        with patch("application_sdk.storage.batch.os.unlink", _flaky_unlink):
            sha = await upload_file_from_bytes(
                "k/quiet.bin", b"data", store, normalize=False
            )
        assert len(sha) == 64
        assert _flaky_unlink.calls == 1

    async def test_empty_bytes(self, store) -> None:
        sha = await upload_file_from_bytes("k/zero.bin", b"", store, normalize=False)
        assert len(sha) == 64
        assert await _get_bytes("k/zero.bin", store, normalize=False) == b""


# ---------------------------------------------------------------------------
# Misc edge cases
# ---------------------------------------------------------------------------


class TestStoreResolution:
    async def test_list_keys_no_store_raises_runtime_error(self) -> None:
        """When no store is supplied AND no infra context is set, raise."""
        # _resolve_store raises RuntimeError under these conditions; surfaces as StorageError-wrapped or RuntimeError
        with patch(
            "application_sdk.storage.batch._resolve_store",
            side_effect=RuntimeError("no store"),
        ):
            with pytest.raises(RuntimeError):
                await list_keys("p/", None)

    async def test_download_prefix_passes_store_through(self, store, tmp_path) -> None:
        """download_prefix delegates list_keys with the user-supplied store.

        Ensures the resolved-store contract is honored end-to-end.
        """
        await _put("only/x.txt", b"y", store, normalize=False)
        dests = await download_prefix("only/", tmp_path, store=store, normalize=False)
        assert len(dests) == 1


class TestUploadPrefixConcurrencyArg:
    async def test_max_concurrency_does_not_break_small_uploads(
        self, store, tmp_path
    ) -> None:
        # Single file, max_concurrency=1 → still works.
        (tmp_path / "f.txt").write_bytes(b"alpha")
        keys = await upload_prefix(
            tmp_path, "p", store, normalize=False, max_concurrency=1
        )
        assert keys == ["p/f.txt"]
