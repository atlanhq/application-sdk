"""Extra unit tests for application_sdk.storage.formats (Reader / Writer base classes).

Targets uncovered branches:
- Reader.close() idempotency and ``_cleanup_downloaded_files`` exception swallow
  (and the inline ``import shutil``).
- Writer._convert_to_dataframe pandas / dict / list / daft / unsupported branches,
  including the daft ImportError fallbacks (BLDX-1129 anchor — inline ``import daft``).
- Writer.write closed / unsupported ``dataframe_type`` errors.
- Writer._write_batched_dataframe rewrap on exception.
- Writer._upload_file delegation and ``retain_local_copy`` propagation.
- Writer._flush_buffer failure path (records error metric, re-raises).
- Writer.close idempotent + rewrap on finalise failure.

All disk I/O outside ``tmp_path`` is mocked; no real obstore traffic; no real
threads or loops beyond pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.common.types import DataframeType
from application_sdk.storage.formats import Reader, WriteMode, Writer

# ---------------------------------------------------------------------------
# Concrete stubs so we can instantiate the abstract classes.
# ---------------------------------------------------------------------------


class _StubReader(Reader):
    def __init__(self, path: str = "/data") -> None:
        # Per-instance state to avoid the shared mutable-default class attribute.
        self.path = path
        self._is_closed = False
        self._downloaded_files: list[str] = []
        self.cleanup_on_close = True

    async def read(self) -> Any:  # pragma: no cover - abstract impl
        return None

    def read_batches(self) -> Any:  # pragma: no cover - abstract impl
        return iter([])


class _StubWriter(Writer):
    """Minimal concrete Writer used to exercise the base methods."""

    def __init__(
        self, path: str = "/tmp/out", dataframe_type=DataframeType.pandas
    ) -> None:
        self.path = path
        self.output_prefix = "out"
        self.total_record_count = 0
        self.chunk_count = 0
        self.chunk_part = 0
        self.buffer_size = 100
        self.max_file_size_bytes = 10_000_000
        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0
        self.partitions: list[int] = []
        self.extension = ".parquet"
        self.dataframe_type = dataframe_type
        self._is_closed = False
        self._statistics = None
        self.chunk_start = None
        self.metrics = MagicMock()

    async def _write_daft_dataframe(self, dataframe: Any, **kwargs: Any) -> None:
        return None

    async def _write_chunk(self, chunk: Any, output_file_name: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Reader: close() idempotency + cleanup
# ---------------------------------------------------------------------------


class TestReaderClose:
    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        reader = _StubReader()
        reader._is_closed = True
        # Should be a no-op even if there are unrelated downloaded files queued.
        reader._downloaded_files = ["/tmp/junk"]
        await reader.close()
        # Files list is untouched because close exits early.
        assert reader._downloaded_files == ["/tmp/junk"]

    @pytest.mark.asyncio
    async def test_close_skips_cleanup_when_disabled(self) -> None:
        reader = _StubReader()
        reader.cleanup_on_close = False
        reader._downloaded_files = ["/tmp/preserved"]
        with patch.object(
            reader, "_cleanup_downloaded_files", new_callable=AsyncMock
        ) as cleanup_mock:
            await reader.close()
        cleanup_mock.assert_not_called()
        assert reader._is_closed is True

    @pytest.mark.asyncio
    async def test_cleanup_swallows_exceptions(self, tmp_path) -> None:
        """Each per-file cleanup error must be logged; loop must not abort."""
        f1 = tmp_path / "a.txt"
        f1.write_text("x")
        f2 = tmp_path / "b.txt"
        f2.write_text("y")

        reader = _StubReader()
        reader._downloaded_files = [str(f1), str(f2)]

        # First call (a.txt) raises; second (b.txt) must still run and succeed.
        original_remove = __import__("os").remove
        calls: list[str] = []

        def _flaky_remove(path: str) -> None:
            calls.append(path)
            if path.endswith("a.txt"):
                raise OSError("permission")
            return original_remove(path)

        with patch(
            "application_sdk.storage.formats.os.remove", side_effect=_flaky_remove
        ):
            await reader._cleanup_downloaded_files()

        assert len(calls) == 2
        assert reader._downloaded_files == []  # cleared regardless of failures

    @pytest.mark.asyncio
    async def test_cleanup_handles_directories_via_inline_shutil(
        self, tmp_path
    ) -> None:
        """Exercises the inline ``import shutil`` for directory cleanup
        (BLDX-1129 anchor)."""
        d = tmp_path / "downloaded_dir"
        d.mkdir()
        (d / "child.txt").write_text("c")

        reader = _StubReader()
        reader._downloaded_files = [str(d)]

        await reader._cleanup_downloaded_files()
        assert not d.exists()
        assert reader._downloaded_files == []

    @pytest.mark.asyncio
    async def test_close_marks_closed_and_clears_files(self, tmp_path) -> None:
        f = tmp_path / "tmp.txt"
        f.write_text("x")
        reader = _StubReader()
        reader._downloaded_files = [str(f)]

        await reader.close()
        assert reader._is_closed is True
        assert not f.exists()


# ---------------------------------------------------------------------------
# Writer._convert_to_dataframe — exercises inline ``import pandas``,
# ``import daft`` (BLDX-1129 anchor) and the daft ImportError fallbacks.
# ---------------------------------------------------------------------------


class TestConvertToDataframe:
    def test_pandas_input_returned_as_is(self) -> None:
        import pandas as pd

        writer = _StubWriter(dataframe_type=DataframeType.pandas)
        df = pd.DataFrame({"a": [1, 2]})
        assert writer._convert_to_dataframe(df) is df

    def test_dict_input_converted_to_pandas(self) -> None:
        import pandas as pd

        writer = _StubWriter(dataframe_type=DataframeType.pandas)
        result = writer._convert_to_dataframe({"col": "v"})
        assert isinstance(result, pd.DataFrame)
        assert result.iloc[0]["col"] == "v"

    def test_list_of_dicts_input_converted_to_pandas(self) -> None:
        import pandas as pd

        writer = _StubWriter(dataframe_type=DataframeType.pandas)
        result = writer._convert_to_dataframe([{"a": 1}, {"a": 2}])
        assert isinstance(result, pd.DataFrame)
        assert list(result["a"]) == [1, 2]

    def test_unsupported_type_raises_typeerror(self) -> None:
        writer = _StubWriter(dataframe_type=DataframeType.pandas)
        with pytest.raises(TypeError, match="Unsupported data type"):
            writer._convert_to_dataframe(42)

    def test_pandas_input_with_daft_type_without_daft_raises(self) -> None:
        """Exercises inline ``import daft`` ImportError fallback."""
        import builtins

        import pandas as pd

        writer = _StubWriter(dataframe_type=DataframeType.daft)
        df = pd.DataFrame({"a": [1]})

        original_import = builtins.__import__

        def _raise_for_daft(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "daft":
                raise ImportError("daft missing")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_raise_for_daft):
            with pytest.raises(TypeError, match="daft is not installed"):
                writer._convert_to_dataframe(df)

    def test_dict_input_with_daft_type_without_daft_raises(self) -> None:
        """Inline ``import daft`` ImportError on dict→daft conversion path."""
        import builtins

        writer = _StubWriter(dataframe_type=DataframeType.daft)
        original_import = builtins.__import__

        def _raise_for_daft(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "daft":
                raise ImportError("daft missing")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_raise_for_daft):
            with pytest.raises(TypeError, match="Dict and list inputs require daft"):
                writer._convert_to_dataframe({"a": 1})


# ---------------------------------------------------------------------------
# Writer.write contract
# ---------------------------------------------------------------------------


class TestWriterWrite:
    @pytest.mark.asyncio
    async def test_write_to_closed_writer_raises(self) -> None:
        writer = _StubWriter()
        writer._is_closed = True
        with pytest.raises(ValueError, match="closed writer"):
            await writer.write({"a": 1})

    @pytest.mark.asyncio
    async def test_write_unsupported_dtype_raises(self) -> None:
        writer = _StubWriter()
        # Replace with an arbitrary value the dispatcher does not understand.
        writer.dataframe_type = "unknown"
        with pytest.raises(ValueError, match="Unsupported dataframe_type"):
            await writer.write({"a": 1})

    @pytest.mark.asyncio
    async def test_write_pandas_calls_internal_method(self) -> None:
        writer = _StubWriter()
        with patch.object(
            writer, "_write_dataframe", new_callable=AsyncMock
        ) as mock_write:
            await writer.write({"a": 1})
        mock_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_daft_calls_internal_method(self) -> None:
        # Force pandas conversion (via dict input) but dispatch to daft branch.
        # Skip if daft not present — simpler: stub _convert_to_dataframe out.
        writer = _StubWriter(dataframe_type=DataframeType.daft)
        sentinel = MagicMock(name="daft_df")
        with (
            patch.object(writer, "_convert_to_dataframe", return_value=sentinel),
            patch.object(
                writer, "_write_daft_dataframe", new_callable=AsyncMock
            ) as mock_daft_write,
        ):
            await writer.write({"a": 1})
        mock_daft_write.assert_awaited_once()


# ---------------------------------------------------------------------------
# Writer.write_batches and _write_batched_dataframe
# ---------------------------------------------------------------------------


class TestWriteBatches:
    @pytest.mark.asyncio
    async def test_write_batches_to_closed_writer_raises(self) -> None:
        writer = _StubWriter()
        writer._is_closed = True

        async def _gen() -> AsyncGenerator[Any, None]:  # pragma: no cover
            yield None

        with pytest.raises(ValueError, match="closed writer"):
            await writer.write_batches(_gen())

    @pytest.mark.asyncio
    async def test_write_batches_unsupported_dtype_raises(self) -> None:
        writer = _StubWriter()
        writer.dataframe_type = "weird"

        def _gen():
            yield None

        with pytest.raises(ValueError, match="Unsupported dataframe_type"):
            await writer.write_batches(_gen())

    @pytest.mark.asyncio
    async def test_batched_dataframe_rewrap_on_failure(self) -> None:
        writer = _StubWriter()
        # _write_dataframe raises during iteration → rewrapped into RewrappedException.
        with patch.object(
            writer,
            "_write_dataframe",
            new_callable=AsyncMock,
            side_effect=RuntimeError("disk error"),
        ):

            def _gen():
                import pandas as pd

                yield pd.DataFrame({"a": [1]})

            with pytest.raises(Exception) as excinfo:
                await writer.write_batches(_gen())
        assert "Error writing batched dataframe" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Writer._upload_file — delegates to _upload_file in ops
# ---------------------------------------------------------------------------


class TestWriterUploadFile:
    @pytest.mark.asyncio
    async def test_upload_file_propagates_retain_local_copy(self) -> None:
        writer = _StubWriter()
        writer.retain_local_copy = False  # type: ignore[attr-defined]
        writer.current_buffer_size_bytes = 999

        with patch(
            "application_sdk.storage.formats._upload_file", new_callable=AsyncMock
        ) as mock_upload:
            await writer._upload_file("/tmp/out/foo.parquet")

        mock_upload.assert_awaited_once_with(
            "/tmp/out/foo.parquet",
            "/tmp/out/foo.parquet",
            retain_local_copy=False,
        )
        assert writer.current_buffer_size_bytes == 0

    @pytest.mark.asyncio
    async def test_upload_file_default_retain_is_false_when_attr_missing(self) -> None:
        """Default for ``retain_local_copy`` is False if the attribute is unset."""
        writer = _StubWriter()
        with patch(
            "application_sdk.storage.formats._upload_file", new_callable=AsyncMock
        ) as mock_upload:
            await writer._upload_file("/tmp/out/foo.parquet")
        mock_upload.assert_awaited_once_with(
            "/tmp/out/foo.parquet",
            "/tmp/out/foo.parquet",
            retain_local_copy=False,
        )


# ---------------------------------------------------------------------------
# Writer._flush_buffer error path
# ---------------------------------------------------------------------------


class TestFlushBuffer:
    @pytest.mark.asyncio
    async def test_flush_buffer_records_error_metric_and_reraises(self) -> None:
        import pandas as pd

        writer = _StubWriter()
        with patch.object(
            writer,
            "_write_chunk",
            new_callable=AsyncMock,
            side_effect=RuntimeError("disk full"),
        ):
            with pytest.raises(RuntimeError, match="disk full"):
                await writer._flush_buffer(pd.DataFrame({"a": [1]}), 0)

        # Last metric call should record write_errors.
        names = [
            c.kwargs.get("name") for c in writer.metrics.record_metric.call_args_list
        ]
        assert "write_errors" in names

    @pytest.mark.asyncio
    async def test_flush_buffer_records_chunks_metric_on_success(self) -> None:
        import pandas as pd

        writer = _StubWriter()
        with patch.object(writer, "_write_chunk", new_callable=AsyncMock):
            await writer._flush_buffer(pd.DataFrame({"a": [1, 2]}), 0)

        # write_chunk metric was recorded.
        names = [
            c.kwargs.get("name") for c in writer.metrics.record_metric.call_args_list
        ]
        assert "chunks_written" in names
        assert writer.total_record_count == 2

    @pytest.mark.asyncio
    async def test_flush_buffer_skips_empty_chunk(self) -> None:
        import pandas as pd

        writer = _StubWriter()
        with patch.object(writer, "_write_chunk", new_callable=AsyncMock) as mock_chunk:
            await writer._flush_buffer(pd.DataFrame({"a": []}), 0)
        mock_chunk.assert_not_awaited()


# ---------------------------------------------------------------------------
# Writer.close — idempotent + rewrap
# ---------------------------------------------------------------------------


class TestWriterClose:
    @pytest.mark.asyncio
    async def test_close_idempotent_returns_cached_statistics(self) -> None:
        from application_sdk.common.models import TaskStatistics

        writer = _StubWriter()
        writer._is_closed = True
        cached = TaskStatistics(total_record_count=5, chunk_count=2, partitions=[0, 1])
        writer._statistics = cached
        result = await writer.close()
        assert result is cached

    @pytest.mark.asyncio
    async def test_close_idempotent_returns_live_statistics_when_no_cache(self) -> None:
        writer = _StubWriter()
        writer._is_closed = True
        writer._statistics = None
        writer.total_record_count = 7
        writer.partitions = [0, 1, 2]
        result = await writer.close()
        assert result.total_record_count == 7
        assert result.chunk_count == 3

    @pytest.mark.asyncio
    async def test_close_rewraps_finalize_failure(self) -> None:
        writer = _StubWriter()
        with patch.object(
            writer,
            "_finalize",
            new_callable=AsyncMock,
            side_effect=RuntimeError("flush fail"),
        ):
            with pytest.raises(Exception) as excinfo:
                await writer.close()
        assert "Error closing writer" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_close_writes_statistics_and_marks_closed(self, tmp_path) -> None:
        writer = _StubWriter(path=str(tmp_path / "out"))
        writer.partitions = [0]
        writer.total_record_count = 4

        with patch(
            "application_sdk.storage.formats._upload_file", new_callable=AsyncMock
        ) as mock_upload:
            stats = await writer.close()

        assert writer._is_closed is True
        assert stats.total_record_count == 4
        # Statistics file uploaded.
        mock_upload.assert_awaited()


# ---------------------------------------------------------------------------
# Writer.statistics property
# ---------------------------------------------------------------------------


class TestStatisticsProperty:
    def test_statistics_reads_live_state(self) -> None:
        writer = _StubWriter()
        writer.total_record_count = 9
        writer.partitions = [0, 1, 2, 3]
        s = writer.statistics
        assert s.total_record_count == 9
        assert s.chunk_count == 4


# ---------------------------------------------------------------------------
# WriteMode enum stability
# ---------------------------------------------------------------------------


class TestWriteMode:
    def test_write_modes(self) -> None:
        assert WriteMode.APPEND.value == "append"
        assert WriteMode.OVERWRITE.value == "overwrite"
        assert WriteMode.OVERWRITE_PARTITIONS.value == "overwrite-partitions"


# ---------------------------------------------------------------------------
# Async context manager round-trip
# ---------------------------------------------------------------------------


class TestContextManagers:
    @pytest.mark.asyncio
    async def test_reader_async_context_manager_closes(self) -> None:
        reader = _StubReader()
        async with reader as r:
            assert r is reader
        assert reader._is_closed is True

    @pytest.mark.asyncio
    async def test_writer_async_context_manager_closes(self, tmp_path) -> None:
        writer = _StubWriter(path=str(tmp_path / "out"))
        with patch(
            "application_sdk.storage.formats._upload_file", new_callable=AsyncMock
        ):
            async with writer as w:
                assert w is writer
        assert writer._is_closed is True


# ---------------------------------------------------------------------------
# SHARED MUTABLE-DEFAULT BUG (skipped). Documented for reviewer.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Reader._downloaded_files is a class-level mutable list. Two instances "
    "that don't reassign in __init__ share state. Flagged in 'Bugs found' for "
    "BLDX-1129; do not modify source."
)
def test_class_level_mutable_default_is_shared() -> None:  # pragma: no cover
    class _Bare(Reader):
        async def read(self):  # pragma: no cover
            return None

        def read_batches(self):  # pragma: no cover
            return iter([])

    a = _Bare()
    b = _Bare()
    a._downloaded_files.append("/tmp/from_a")
    assert "/tmp/from_a" not in b._downloaded_files
