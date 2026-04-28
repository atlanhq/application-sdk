"""Extra unit tests for application_sdk.storage.ops.

These tests focus on the uncovered branches of ops.py:
- The lazy ``_log()`` logger initialisation (inline import).
- The lazy resolve-store failure path (inline import of get_infrastructure).
- ``_compute_part_size`` for the S3 10 000-part safety branch.
- ``_is_not_found`` matrix.
- Inline-import error wrapping in upload_file / download_file / _put / _get_bytes /
  delete / exists (BLDX-1129 anchor: each error path imports StorageError /
  StorageNotFoundError lazily).

All real I/O is mocked; no real obstore traffic, no threads, no event loops
beyond the per-test asyncio loop pytest-asyncio creates.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.storage import ops as ops_module
from application_sdk.storage.errors import StorageError, StorageNotFoundError
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
        """Exercises the lazy ``get_logger`` import in ``_log()`` (BLDX-1129 anchor)."""
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
        with patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="No ObjectStore provided"):
                _resolve_store(None)

    def test_raises_when_infra_storage_is_none(self) -> None:
        infra = SimpleNamespace(storage=None)
        with patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=infra,
        ):
            with pytest.raises(RuntimeError, match="No ObjectStore provided"):
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
# Error wrapping branches: each one exercises an inline import of StorageError
# (BLDX-1129 anchor).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_wraps_failure_as_storage_error() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.put_async",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(StorageError) as excinfo:
            await _put("k", b"v", store, normalize=False)
    assert excinfo.value.key == "k"
    assert isinstance(excinfo.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_get_bytes_wraps_non_404_as_storage_error() -> None:
    store = MagicMock()
    with patch(
        "application_sdk.storage.ops.obstore.get_async",
        new=AsyncMock(side_effect=RuntimeError("permission denied")),
    ):
        with pytest.raises(StorageError):
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
    with patch(
        "application_sdk.storage.ops.obstore.delete_async",
        new=AsyncMock(side_effect=RuntimeError("permission denied")),
    ):
        with pytest.raises(StorageError):
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
    with patch(
        "application_sdk.storage.ops.obstore.head_async",
        new=AsyncMock(side_effect=RuntimeError("server error")),
    ):
        with pytest.raises(StorageError):
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
    """The writer raises mid-upload; ops must wrap as StorageError (BLDX-1129 anchor)."""
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")

    class _BoomCM:
        async def __aenter__(self) -> None:
            raise RuntimeError("network")

        async def __aexit__(self, *exc) -> None:  # pragma: no cover - not reached
            return None

    with patch(
        "application_sdk.storage.ops.obstore.open_writer_async",
        return_value=_BoomCM(),
    ):
        with pytest.raises(StorageError) as excinfo:
            await upload_file("k", f, MagicMock(), normalize=False)
    assert excinfo.value.key == "k"


@pytest.mark.asyncio
async def test_download_file_translates_404_to_not_found(tmp_path) -> None:
    """Download must translate a 404 from obstore into StorageNotFoundError."""
    with patch(
        "application_sdk.storage.ops.obstore.get_async",
        new=AsyncMock(side_effect=RuntimeError("Key not found")),
    ):
        with pytest.raises(StorageNotFoundError):
            await download_file("k", tmp_path / "out.bin", MagicMock(), normalize=False)


@pytest.mark.asyncio
async def test_download_file_wraps_non_404_get_failure(tmp_path) -> None:
    with patch(
        "application_sdk.storage.ops.obstore.get_async",
        new=AsyncMock(side_effect=RuntimeError("permission denied")),
    ):
        with pytest.raises(StorageError) as excinfo:
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

    with patch(
        "application_sdk.storage.ops.obstore.get_async",
        new=AsyncMock(return_value=result_obj),
    ):
        with pytest.raises(StorageError):
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
