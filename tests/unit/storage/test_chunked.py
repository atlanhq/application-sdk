"""Unit tests for TimeChunkedWriter.

Exercises:
- Scoped writer-owned sub-directory
- append() buffers, flush() / __aexit__ rolls to a chunk file
- Time-based rollover (mocked clock, no sleep)
- Async vs sync flush_fn dispatch
- on_chunk_complete callback fires per chunk and survives exceptions
- file_reference property returns ephemeral FileReference scoped to subdir
- Validation: empty base_path, missing-leading-dot extension, non-positive interval
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.contracts.types import FileReference
from application_sdk.storage.chunked import TimeChunkedWriter


@pytest.fixture
def base(tmp_path: Path) -> str:
    """Base directory under which the writer creates its scoped subdir."""
    return str(tmp_path / "base")


def _make_flush_fn():
    """Sync flush_fn that records (batches, path) calls and writes a stamp file."""
    calls: list[tuple[list[Any], str]] = []

    def _flush(batches: list[Any], path: str) -> None:
        calls.append((list(batches), path))
        # Touch the file so disk-existence assertions are meaningful.
        with open(path, "w") as f:
            f.write(f"chunk:{len(batches)}")

    return _flush, calls


class TestTimeChunkedWriterInit:
    def test_scoped_subdir_created_under_base(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = TimeChunkedWriter(base, ".parquet", flush_fn)
        assert writer.output_dir.startswith(base + os.sep)
        assert os.path.basename(writer.output_dir).startswith("chunked_")
        assert os.path.isdir(writer.output_dir)

    def test_explicit_subdir_name(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = TimeChunkedWriter(
            base, ".parquet", flush_fn, scoped_subdir_name="fixed"
        )
        assert writer.output_dir == os.path.join(base, "fixed")
        assert os.path.isdir(writer.output_dir)

    def test_rejects_empty_base_path(self) -> None:
        flush_fn, _ = _make_flush_fn()
        with pytest.raises(ValueError, match="base_path"):
            TimeChunkedWriter("", ".parquet", flush_fn)

    def test_rejects_extension_without_leading_dot(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        with pytest.raises(ValueError, match="must start with"):
            TimeChunkedWriter(base, "parquet", flush_fn)

    def test_rejects_non_positive_interval(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        with pytest.raises(ValueError, match="chunk_interval_seconds"):
            TimeChunkedWriter(base, ".parquet", flush_fn, chunk_interval_seconds=0)


class TestTimeChunkedWriterFlush:
    @pytest.mark.asyncio
    async def test_aexit_flushes_buffer(self, base: str) -> None:
        """Pending appends must reach disk on context-manager exit."""
        flush_fn, calls = _make_flush_fn()
        async with TimeChunkedWriter(
            base, ".json", flush_fn, scoped_subdir_name="run"
        ) as writer:
            await writer.append({"id": 1})
            await writer.append({"id": 2})

        assert len(calls) == 1
        batches, path = calls[0]
        assert batches == [{"id": 1}, {"id": 2}]
        assert path == os.path.join(base, "run", "chunk-0.json")
        assert os.path.exists(path)

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, base: str) -> None:
        flush_fn, calls = _make_flush_fn()
        writer = TimeChunkedWriter(base, ".json", flush_fn)
        await writer.append({"id": 1})
        await writer.close()
        await writer.close()  # must not re-flush or raise
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_append_after_close_raises(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = TimeChunkedWriter(base, ".json", flush_fn)
        await writer.close()
        with pytest.raises(RuntimeError, match="closed"):
            await writer.append({"id": 1})

    @pytest.mark.asyncio
    async def test_explicit_flush_starts_new_chunk(self, base: str) -> None:
        flush_fn, calls = _make_flush_fn()
        async with TimeChunkedWriter(
            base, ".json", flush_fn, scoped_subdir_name="run"
        ) as writer:
            await writer.append({"id": 1})
            await writer.flush()
            await writer.append({"id": 2})

        # Two chunks: one from explicit flush, one from __aexit__.
        assert len(calls) == 2
        assert calls[0][0] == [{"id": 1}]
        assert calls[0][1].endswith("chunk-0.json")
        assert calls[1][0] == [{"id": 2}]
        assert calls[1][1].endswith("chunk-1.json")

    @pytest.mark.asyncio
    async def test_empty_buffer_flush_is_noop(self, base: str) -> None:
        flush_fn, calls = _make_flush_fn()
        async with TimeChunkedWriter(base, ".json", flush_fn) as writer:
            await writer.flush()  # nothing buffered
        assert calls == []
        assert writer.chunk_count == 0

    @pytest.mark.asyncio
    async def test_async_flush_fn(self, base: str) -> None:
        """flush_fn may be an async callable; the writer awaits it."""
        calls: list[tuple[list[Any], str]] = []

        async def _flush(batches: list[Any], path: str) -> None:
            calls.append((list(batches), path))
            with open(path, "w") as f:
                f.write("ok")

        async with TimeChunkedWriter(base, ".json", _flush) as writer:
            await writer.append({"id": 1})

        assert len(calls) == 1
        assert calls[0][0] == [{"id": 1}]


class TestTimeChunkedWriterRollover:
    @pytest.mark.asyncio
    async def test_time_rollover_after_interval_elapsed(self, base: str) -> None:
        """append after interval elapses must flush the in-flight chunk."""
        flush_fn, calls = _make_flush_fn()
        # Drive the monotonic clock deterministically.
        clock = iter([100.0, 100.5, 161.0, 161.5])
        with patch(
            "application_sdk.storage.chunked.time.monotonic", lambda: next(clock)
        ):
            async with TimeChunkedWriter(
                base,
                ".json",
                flush_fn,
                chunk_interval_seconds=60.0,
                scoped_subdir_name="run",
            ) as writer:
                # First append at t=100 — starts chunk-0 timer.
                # Read inside append: start=100.0, then elapsed-check at 100.5
                # (elapsed=0.5, under 60).
                await writer.append({"id": 1})
                # Second append at t=161 — start-check sees existing start,
                # then elapsed-check at 161.5 (elapsed=61.5, over 60) → flush
                # chunk-0 with [{1}, {2}].
                await writer.append({"id": 2})

        # Exactly one rollover from the timer; __aexit__ found empty buffer
        # because the second append's payload was flushed already.
        flushed_paths = [c[1] for c in calls]
        assert flushed_paths == [os.path.join(base, "run", "chunk-0.json")]
        assert calls[0][0] == [{"id": 1}, {"id": 2}]

    @pytest.mark.asyncio
    async def test_short_interval_rolls_between_sleeps(self, base: str) -> None:
        """Wall-clock interval drives rollover for real (non-mocked) sleeps.

        Semantics: append() buffers the new item, then checks elapsed. So the
        first batch that crosses the interval includes both the previously
        buffered items and the current append, and the next chunk starts
        fresh on the *next* append after that.
        """
        import asyncio

        flush_fn, calls = _make_flush_fn()
        async with TimeChunkedWriter(
            base,
            ".json",
            flush_fn,
            chunk_interval_seconds=0.005,
            scoped_subdir_name="run",
        ) as writer:
            await writer.append({"id": 1})  # starts chunk-0 timer
            await asyncio.sleep(0.02)
            await writer.append({"id": 2})  # crosses interval → flushes [1,2]
            await asyncio.sleep(0.02)
            await writer.append({"id": 3})  # starts chunk-1 timer
            await asyncio.sleep(0.02)
            await writer.append({"id": 4})  # crosses again → flushes [3,4]
            # __aexit__ then flushes any final buffer.

        paths = sorted(p for _, p in calls)
        assert any(p.endswith("chunk-0.json") for p in paths)
        assert any(p.endswith("chunk-1.json") for p in paths)
        # All four items written exactly once, in order.
        flushed_items: list[Any] = []
        for batches, _ in calls:
            flushed_items.extend(batches)
        assert flushed_items == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]


class TestTimeChunkedWriterOnChunkComplete:
    @pytest.mark.asyncio
    async def test_callback_fires_per_chunk_with_index_and_path(
        self, base: str
    ) -> None:
        flush_fn, _ = _make_flush_fn()
        cb = MagicMock()
        async with TimeChunkedWriter(
            base,
            ".json",
            flush_fn,
            on_chunk_complete=cb,
            scoped_subdir_name="run",
        ) as writer:
            await writer.append({"id": 1})
            await writer.flush()
            await writer.append({"id": 2})

        assert cb.call_count == 2
        first_idx, first_path = cb.call_args_list[0].args
        second_idx, second_path = cb.call_args_list[1].args
        assert (first_idx, second_idx) == (0, 1)
        assert first_path.endswith("chunk-0.json")
        assert second_path.endswith("chunk-1.json")

    @pytest.mark.asyncio
    async def test_async_callback_supported(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        cb = AsyncMock()
        async with TimeChunkedWriter(
            base, ".json", flush_fn, on_chunk_complete=cb
        ) as writer:
            await writer.append({"id": 1})
        assert cb.await_count == 1

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_abort_writer(self, base: str) -> None:
        """on_chunk_complete is best-effort; raises in it must not bubble."""
        flush_fn, calls = _make_flush_fn()
        cb = MagicMock(side_effect=RuntimeError("heartbeat down"))
        async with TimeChunkedWriter(
            base, ".json", flush_fn, on_chunk_complete=cb
        ) as writer:
            await writer.append({"id": 1})
            await writer.append({"id": 2})

        # Chunk still landed despite the failing callback.
        assert len(calls) == 1
        assert cb.call_count == 1


class TestTimeChunkedWriterFileReference:
    def test_file_reference_points_at_scoped_subdir(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = TimeChunkedWriter(base, ".json", flush_fn, scoped_subdir_name="run")
        ref = writer.file_reference
        assert isinstance(ref, FileReference)
        assert ref.local_path == os.path.join(base, "run")
        assert ref.is_durable is False  # ephemeral until persisted

    @pytest.mark.asyncio
    async def test_file_reference_after_writes_covers_only_writer_dir(
        self, tmp_path: Path
    ) -> None:
        """Sibling content in the parent must not be inside the ref."""
        base = tmp_path / "shared"
        base.mkdir()
        sibling = base / "do_not_upload.txt"
        sibling.write_text("hands off")

        flush_fn, _ = _make_flush_fn()
        async with TimeChunkedWriter(
            str(base), ".json", flush_fn, scoped_subdir_name="run"
        ) as writer:
            await writer.append({"id": 1})

        ref = writer.file_reference
        assert ref.local_path == str(base / "run")
        # Files under the ref do not include the sibling.
        dir_files = list(Path(ref.local_path).rglob("*"))
        assert all("do_not_upload" not in f.name for f in dir_files)
        # Sibling is still where we put it.
        assert sibling.exists()
        assert sibling.read_text() == "hands off"
