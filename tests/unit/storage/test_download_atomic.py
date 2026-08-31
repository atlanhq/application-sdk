"""Regression coverage for atomic single-stream downloads."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.storage.errors import StorageError
from application_sdk.storage.ops import download_file


async def test_download_file_stream_failure_preserves_existing_artifact(
    tmp_path,
) -> None:
    """A failed streamed download must not truncate a complete prior artifact."""

    class _PartialThenBoomStream:
        def __init__(self) -> None:
            self._chunks = iter((b"replacement", RuntimeError("stream interrupted")))

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = next(self._chunks)
            if isinstance(item, Exception):
                raise item
            return item

    destination = tmp_path / "out.bin"
    destination.write_bytes(b"complete prior artifact")
    result_obj = MagicMock()
    result_obj.stream.return_value = _PartialThenBoomStream()

    with (
        patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(return_value=result_obj),
        ),
        pytest.raises(StorageError, match="Failed to write downloaded file"),
    ):
        await download_file("k", destination, MagicMock(), normalize=False)

    assert destination.read_bytes() == b"complete prior artifact"


async def test_download_file_offloads_fsync_through_async_atomic_write(
    tmp_path,
) -> None:
    """The shared async writer keeps final flush work off the event loop."""

    class _OneChunkStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if hasattr(self, "_sent"):
                raise StopAsyncIteration
            self._sent = True
            return b"complete replacement"

    destination = tmp_path / "out.bin"
    result_obj = MagicMock()
    result_obj.meta = {"size": len(b"complete replacement")}
    result_obj.stream.return_value = _OneChunkStream()
    offload = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    with (
        patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(return_value=result_obj),
        ),
        patch("application_sdk.common.atomic.run_in_thread", new=offload),
    ):
        await download_file("k", destination, MagicMock(), normalize=False)

    assert destination.read_bytes() == b"complete replacement"
    assert offload.await_count == 1
    assert offload.await_args.args[0] is os.fsync
