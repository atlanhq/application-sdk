"""Unit tests for application_sdk.storage.ops.

These tests focus on the uncovered branches of ops.py:
- The lazy ``_log()`` logger initialisation (inline import).
- The lazy resolve-store failure path (inline import of get_infrastructure).
- ``_compute_part_size`` for the S3 10 000-part safety branch.
- ``_is_not_found`` matrix.
- Inline-import error wrapping in upload_file / download_file / _put / _get_bytes /
  delete / exists (each error path imports StorageError / StorageNotFoundError lazily).

All real I/O is mocked; no real obstore traffic, no threads, no event loops
beyond the per-test asyncio loop pytest-asyncio creates.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.storage import ops as ops_module
from application_sdk.storage.batch import delete_prefix, list_keys
from application_sdk.storage.errors import StorageError, StorageNotFoundError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import (
    _compute_part_size,
    _get_bytes,
    _is_not_found,
    _log,
    _put,
    _resolve_store,
    delete,
    download_file,
    exists,
    upload_file,
)


@pytest.fixture
def store():
    return create_memory_store()


# ---------------------------------------------------------------------------
# _log() lazy logger initialisation (inline import)
# ---------------------------------------------------------------------------


class TestLazyLog:
    def setup_method(self) -> None:
        # Force re-initialisation so we can verify the inline import path runs.
        ops_module._logger = None

    def teardown_method(self) -> None:
        ops_module._logger = None

    def test_log_initialises_logger_via_inline_import(self) -> None:
        """Exercises the lazy ``get_logger`` import in ``_log()``."""
        sentinel_logger = MagicMock()
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=sentinel_logger,
        ) as mock_get_logger:
            result = _log()
            # Calling twice must not re-import.
            result_second = _log()

        assert result is sentinel_logger
        assert result_second is sentinel_logger
        mock_get_logger.assert_called_once_with("application_sdk.storage.ops")


# ---------------------------------------------------------------------------
# _resolve_store
# ---------------------------------------------------------------------------


class TestResolveStore:
    def test_returns_explicit_store(self) -> None:
        store = MagicMock()
        assert _resolve_store(store) is store

    def test_raises_runtime_error_when_no_infra(self) -> None:
        """Exercises the inline import of ``get_infrastructure`` and the error branch."""
        with (
            patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(RuntimeError, match="No ObjectStore provided"),
        ):
            _resolve_store(None)

    def test_raises_when_infra_storage_is_none(self) -> None:
        infra = SimpleNamespace(storage=None)
        with (
            patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
            pytest.raises(RuntimeError, match="No ObjectStore provided"),
        ):
            _resolve_store(None)

    def test_returns_infra_storage_when_set(self) -> None:
        store = MagicMock()
        infra = SimpleNamespace(storage=store)
        with patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=infra,
        ):
            assert _resolve_store(None) is store


# ---------------------------------------------------------------------------
# delete_prefix: GCS directory marker handling
# ---------------------------------------------------------------------------


class TestDeletePrefix:
    async def test_delete_prefix_removes_intermediate_directory_markers(
        self, store
    ) -> None:
        # The key scenario: "artifacts/run" is a GCS directory marker (zero-byte,
        # trailing slash stripped by obstore) that sits WITHIN the deletion prefix
        # "artifacts/". list_keys would filter it out; delete_prefix must not.
        await _put("artifacts/run/file.json", b"{}", store, normalize=False)
        await _put("artifacts/run", b"", store, normalize=False)  # GCS marker

        deleted = await delete_prefix(
            "artifacts", store
        )  # normalize=True → "artifacts/"

        assert deleted == 2  # file + intermediate marker both removed
        assert (
            await _get_bytes("artifacts/run/file.json", store, normalize=False) is None
        )
        assert await _get_bytes("artifacts/run", store, normalize=False) is None

    async def test_delete_prefix_nested_markers_all_removed(self, store) -> None:
        # Nested markers: "a/b" and "a/b/c" are both zero-byte parents within "a/".
        await _put("a/b/c/file.json", b"{}", store, normalize=False)
        await _put("a/b/c", b"", store, normalize=False)  # nested marker
        await _put("a/b", b"", store, normalize=False)  # outer marker

        deleted = await delete_prefix("a", store)

        assert deleted == 3
        assert await _get_bytes("a/b/c/file.json", store, normalize=False) is None
        assert await _get_bytes("a/b/c", store, normalize=False) is None
        assert await _get_bytes("a/b", store, normalize=False) is None

    async def test_delete_prefix_zero_byte_leaf_file_removed(self, store) -> None:
        # A legitimate zero-byte file (no children) must also be deleted.
        await _put("data/empty.json", b"", store, normalize=False)
        await _put("data/full.json", b"{}", store, normalize=False)

        deleted = await delete_prefix("data", store, normalize=False)

        assert deleted == 2
        assert await _get_bytes("data/empty.json", store, normalize=False) is None
        assert await _get_bytes("data/full.json", store, normalize=False) is None

    async def test_delete_prefix_removes_root_directory_marker(self, store) -> None:
        # Root-marker case: "artifacts/run" (the GCS marker) is at the same path
        # as the requested prefix itself after normalisation. It lies outside the
        # "artifacts/run/" listing prefix, so it must be swept by the best-effort
        # root-marker delete rather than the main batch.
        await _put("artifacts/run/file.json", b"{}", store, normalize=False)
        await _put("artifacts/run", b"", store, normalize=False)  # GCS root marker

        deleted = await delete_prefix(
            "artifacts/run", store
        )  # → prefix "artifacts/run/"

        assert deleted == 2  # file + root marker both removed
        assert (
            await _get_bytes("artifacts/run/file.json", store, normalize=False) is None
        )
        assert await _get_bytes("artifacts/run", store, normalize=False) is None


# ---------------------------------------------------------------------------
# list_keys: GCS directory marker handling
# ---------------------------------------------------------------------------


class TestListKeys:
    async def test_list_all_keys(self, store) -> None:
        await _put("a/b.txt", b"1", store)
        await _put("a/c.txt", b"2", store)
        keys = await list_keys(store=store)
        assert "a/b.txt" in keys
        assert "a/c.txt" in keys

    async def test_list_keys_suffix_filter_case_insensitive(self, store) -> None:
        await _put("data/upper.JSON", b"u", store, normalize=False)
        await _put("data/lower.json", b"l", store, normalize=False)
        await _put("data/skip.txt", b"s", store, normalize=False)

        keys = await list_keys("data", store, suffix=".json")
        assert len(keys) == 2
        assert "data/upper.JSON" in keys
        assert "data/lower.json" in keys
        assert "data/skip.txt" not in keys

    async def test_list_keys_excludes_zero_byte_directory_markers(self, store) -> None:
        # GCS strips the trailing slash from directory markers so they appear as
        # bare keys when listing a *parent* prefix (e.g. "run" instead of "run/").
        # List from an ancestor prefix ("") so the bare marker enters all_items.
        await _put("run/manifest.json", b'{"version":1}', store, normalize=False)
        await _put("run/catalog.json", b'{"sources":{}}', store, normalize=False)
        # Simulate a GCS directory marker: 0-byte object at the bare prefix key.
        await _put("run", b"", store, normalize=False)

        # Use normalize=False + empty prefix so "run" (the marker) appears in
        # all_items and the parent_dirs filter has something to act on.
        keys = await list_keys("", store, normalize=False)
        assert "run/manifest.json" in keys
        assert "run/catalog.json" in keys
        # The zero-byte marker must not appear in results.
        assert "run" not in keys
        assert len(keys) == 2

    async def test_list_keys_retains_zero_byte_file_with_no_children(
        self, store
    ) -> None:
        # A zero-byte file that is NOT a parent of any other key is a real
        # empty file and must be returned by list_keys.
        await _put("data/results.json", b"{}", store, normalize=False)
        await _put("data/empty.json", b"", store, normalize=False)

        keys = await list_keys("data", store, suffix=".json")
        assert "data/results.json" in keys
        # zero-byte but no children → legitimate file, must be included
        assert "data/empty.json" in keys
        assert len(keys) == 2

    async def test_list_keys_excludes_nested_directory_markers(self, store) -> None:
        # Nested GCS markers: both "run/sub" and "run" are 0-byte parent objects.
        # List from ancestor prefix so both bare markers enter all_items.
        await _put("run/sub/file.json", b'{"x":1}', store, normalize=False)
        await _put("run/sub", b"", store, normalize=False)  # nested dir marker
        await _put("run", b"", store, normalize=False)  # top-level dir marker

        keys = await list_keys("", store, normalize=False)
        assert "run/sub/file.json" in keys
        assert "run/sub" not in keys
        assert "run" not in keys
        assert len(keys) == 1

    async def test_list_keys_excludes_intermediate_directory_markers(
        self, store
    ) -> None:
        # The core scenario parent_dirs is designed for: listing under a broad
        # prefix returns "artifacts/run" (the obstore-stripped form of the GCS
        # "artifacts/run/" marker) because it starts with "artifacts/".  The
        # prefix filter does not help here — only parent_dirs catches it.
        await _put("artifacts/run/file.json", b"{}", store, normalize=False)
        await _put("artifacts/run", b"", store, normalize=False)

        keys = await list_keys("artifacts", store)
        assert "artifacts/run/file.json" in keys
        assert "artifacts/run" not in keys
        assert len(keys) == 1

    async def test_list_keys_zero_byte_sibling_not_filtered_as_marker(
        self, store
    ) -> None:
        # "a/b" is zero-byte but is a SIBLING of "a/c", not a parent of any listed
        # key.  parent_dirs = {"a"} — "a/b" is not in parent_dirs → must be
        # returned as a legitimate empty file.
        await _put("a/b", b"", store, normalize=False)
        await _put("a/c", b"data", store, normalize=False)

        keys = await list_keys("", store, normalize=False)
        assert "a/b" in keys  # zero-byte but no children → retained
        assert "a/c" in keys
        assert len(keys) == 2


# ---------------------------------------------------------------------------
# _compute_part_size: 10 000-part safety floor
# ---------------------------------------------------------------------------


class TestComputePartSize:
    def test_small_file_returns_chunk_size(self) -> None:
        assert _compute_part_size(file_size=1024, chunk_size=8 * 1024 * 1024) == (
            8 * 1024 * 1024
        )

    def test_huge_file_increases_part_size(self) -> None:
        # 100 GiB with default 8 MiB part would need >9900 parts.
        file_size = 100 * 1024 * 1024 * 1024
        chunk = 8 * 1024 * 1024
        result = _compute_part_size(file_size, chunk)
        assert result > chunk
        # Confirm 9900-part safety: file_size / result <= 9900.
        assert file_size / result <= 9900 + 1  # rounding tolerance


# ---------------------------------------------------------------------------
# _is_not_found
# ---------------------------------------------------------------------------


class TestIsNotFound:
    @pytest.mark.parametrize(
        "msg",
        [
            "Key not found",
            "404",
            "no such file or directory",
            "object does not exist",
            "Item NOT FOUND",
        ],
    )
    def test_matches(self, msg: str) -> None:
        assert _is_not_found(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        ["permission denied", "bad credentials", "internal server error"],
    )
    def test_does_not_match(self, msg: str) -> None:
        assert _is_not_found(Exception(msg)) is False


# ---------------------------------------------------------------------------
# Error wrapping branches: each one exercises an inline import of StorageError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_wraps_failure_as_storage_error() -> None:
    store = MagicMock()
    with (
        patch(
            "application_sdk.storage.ops.obstore.put_async",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(StorageError) as excinfo,
    ):
        await _put("k", b"v", store, normalize=False)
    assert excinfo.value.key == "k"
    assert isinstance(excinfo.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_get_bytes_wraps_non_404_as_storage_error() -> None:
    store = MagicMock()
    with (
        patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(side_effect=RuntimeError("permission denied")),
        ),
        pytest.raises(StorageError),
    ):
        await _get_bytes("k", store, normalize=False)


@pytest.mark.asyncio
async def test_get_bytes_returns_none_on_404() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.get_async",
        new=AsyncMock(side_effect=RuntimeError("not found")),
    ):
        assert await _get_bytes("missing", store, normalize=False) is None


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_delete_returns_false_on_404() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.delete_async",
        new=AsyncMock(side_effect=RuntimeError("404")),
    ):
        assert await delete("k", store, normalize=False) is False


@pytest.mark.asyncio
async def test_exists_returns_false_on_404() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.head_async",
        new=AsyncMock(side_effect=RuntimeError("not found")),
    ):
        assert await exists("k", store, normalize=False) is False


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_exists_returns_true_on_success() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.head_async",
        new=AsyncMock(return_value=MagicMock()),
    ):
        assert await exists("k", store, normalize=False) is True


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_upload_file_does_not_delete_when_retain_local_copy_true(
    tmp_path,
) -> None:
    """Retain flag honoured even when path is inside staging."""
    f = tmp_path / "kept.bin"
    f.write_bytes(b"data")

    async def _writer_factory(*_args, **_kwargs):
        class _CM:
            async def __aenter__(self):
                return AsyncMock(write=AsyncMock())

            async def __aexit__(self, *exc):
                return None

        return _CM()

    with patch(
        "application_sdk.storage.ops.obstore.open_writer_async",
        side_effect=lambda *a, **k: _writer_factory().__await__().__next__(),
    ):
        # Mock the writer cleaner: actually swap the implementation.
        pass

    # Use a simpler alternative path: patch open_writer_async to return an async CM directly
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
