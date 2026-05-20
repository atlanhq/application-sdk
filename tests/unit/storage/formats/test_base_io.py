"""Unit tests for application_sdk.storage.formats (Reader / Writer base classes).

Targets uncovered branches:
- Reader.close() idempotency and ``_cleanup_downloaded_files`` exception swallow
  (and the inline ``import shutil``).
- Writer._convert_to_dataframe pandas / dict / list / daft / unsupported branches,
  including the daft ImportError fallbacks.
- Writer.write closed / unsupported ``dataframe_type`` errors.
- Writer._write_batched_dataframe raises FormatWriteError on exception.
- Writer._upload_file delegation and ``retain_local_copy`` propagation.
- Writer._flush_buffer failure path (records error metric, re-raises).
- Writer.close idempotent + raises FormatCloseError on finalise failure.

All disk I/O outside ``tmp_path`` is mocked; no real obstore traffic; no real
threads or loops beyond pytest-asyncio.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
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
        self._result = None
        self.chunk_start = None
        self.typename = None
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
        """Exercises the inline ``import shutil`` for directory cleanup."""
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
# Writer._convert_to_dataframe — exercises inline ``import pandas`` and
# ``import daft`` plus the daft ImportError fallbacks.
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
        from application_sdk.storage.formats.format_errors import (
            UnsupportedDataTypeError,
        )

        writer = _StubWriter(dataframe_type=DataframeType.pandas)
        with pytest.raises(UnsupportedDataTypeError) as exc_info:
            writer._convert_to_dataframe(42)
        assert exc_info.value.code == "INVALID_INPUT_FORMAT_DATA_TYPE"

    def test_pandas_input_with_daft_type_without_daft_raises(self) -> None:
        """Exercises inline ``import daft`` ImportError fallback."""
        import builtins

        import pandas as pd

        from application_sdk.storage.formats.format_errors import DaftNotInstalledError

        writer = _StubWriter(dataframe_type=DataframeType.daft)
        df = pd.DataFrame({"a": [1]})

        original_import = builtins.__import__

        def _raise_for_daft(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "daft":
                raise ImportError("daft missing")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_raise_for_daft):
            with pytest.raises(DaftNotInstalledError) as exc_info:
                writer._convert_to_dataframe(df)
        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_DAFT"

    def test_dict_input_with_daft_type_without_daft_raises(self) -> None:
        """Inline ``import daft`` ImportError on dict→daft conversion path."""
        import builtins

        from application_sdk.storage.formats.format_errors import DaftNotInstalledError

        writer = _StubWriter(dataframe_type=DataframeType.daft)
        original_import = builtins.__import__

        def _raise_for_daft(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "daft":
                raise ImportError("daft missing")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_raise_for_daft):
            with pytest.raises(DaftNotInstalledError) as exc_info:
                writer._convert_to_dataframe({"a": 1})
        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_DAFT"


# ---------------------------------------------------------------------------
# Writer.write contract
# ---------------------------------------------------------------------------


class TestWriterWrite:
    @pytest.mark.asyncio
    async def test_write_to_closed_writer_raises(self) -> None:
        from application_sdk.storage.formats.format_errors import WriterClosedError

        writer = _StubWriter()
        writer._is_closed = True
        with pytest.raises(WriterClosedError) as exc_info:
            await writer.write({"a": 1})
        assert exc_info.value.code == "PRECONDITION_FORMAT_WRITER_CLOSED"

    @pytest.mark.asyncio
    async def test_write_unsupported_dtype_raises(self) -> None:
        from application_sdk.storage.formats.format_errors import (
            UnsupportedDataframeTypeError,
        )

        writer = _StubWriter()
        # Replace with an arbitrary value the dispatcher does not understand.
        writer.dataframe_type = "unknown"
        with pytest.raises(UnsupportedDataframeTypeError) as exc_info:
            await writer.write({"a": 1})
        assert exc_info.value.code == "UNIMPLEMENTED_FORMAT_DATAFRAME_TYPE"

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
        from application_sdk.storage.formats.format_errors import WriterClosedError

        writer = _StubWriter()
        writer._is_closed = True

        async def _gen() -> AsyncGenerator[Any, None]:  # pragma: no cover
            yield None

        with pytest.raises(WriterClosedError) as exc_info:
            await writer.write_batches(_gen())
        assert exc_info.value.code == "PRECONDITION_FORMAT_WRITER_CLOSED"

    @pytest.mark.asyncio
    async def test_write_batches_unsupported_dtype_raises(self) -> None:
        from application_sdk.storage.formats.format_errors import (
            UnsupportedDataframeTypeError,
        )

        writer = _StubWriter()
        writer.dataframe_type = "weird"

        def _gen():
            yield None

        with pytest.raises(UnsupportedDataframeTypeError) as exc_info:
            await writer.write_batches(_gen())
        assert exc_info.value.code == "UNIMPLEMENTED_FORMAT_DATAFRAME_TYPE"

    @pytest.mark.asyncio
    async def test_batched_dataframe_raises_format_write_error(self) -> None:
        from application_sdk.storage.formats.format_errors import FormatWriteError

        writer = _StubWriter()
        # _write_dataframe raises during iteration → wrapped into FormatWriteError.
        with patch.object(
            writer,
            "_write_dataframe",
            new_callable=AsyncMock,
            side_effect=RuntimeError("disk error"),
        ):

            def _gen():
                import pandas as pd

                yield pd.DataFrame({"a": [1]})

            with pytest.raises(FormatWriteError) as excinfo:
                await writer.write_batches(_gen())
        assert excinfo.value.code == "INTERNAL_FORMAT_WRITE"


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
        with (
            patch.object(
                writer,
                "_write_chunk",
                new_callable=AsyncMock,
                side_effect=RuntimeError("disk full"),
            ),
            pytest.raises(RuntimeError, match="disk full"),
        ):
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
# Writer.close — idempotent + FormatCloseError
# ---------------------------------------------------------------------------


class TestWriterClose:
    @pytest.mark.asyncio
    async def test_close_idempotent_returns_cached_result(self) -> None:
        from application_sdk.storage.formats import WriterResult

        writer = _StubWriter()
        writer._is_closed = True
        cached = WriterResult(
            total_record_count=5,
            chunk_count=2,
            partitions=[0, 1],
            files=None,
        )
        writer._result = cached
        result = await writer.close()
        assert result is cached

    @pytest.mark.asyncio
    async def test_close_idempotent_rederives_when_no_cached_result(self) -> None:
        writer = _StubWriter()
        writer._is_closed = True
        writer._statistics = None
        writer._result = None
        writer.total_record_count = 7
        writer.partitions = [0, 1, 2]
        result = await writer.close()
        # WriterResult subclasses TaskStatistics, so attribute access is direct.
        assert result.total_record_count == 7
        assert result.chunk_count == 3
        # Base Writer doesn't opt into deferred uploads → no FileReference.
        assert result.files is None

    @pytest.mark.asyncio
    async def test_close_raises_format_close_error_on_finalize_failure(self) -> None:
        from application_sdk.storage.formats.format_errors import FormatCloseError

        writer = _StubWriter()
        with (
            patch.object(
                writer,
                "_finalize",
                new_callable=AsyncMock,
                side_effect=RuntimeError("flush fail"),
            ),
            pytest.raises(FormatCloseError) as excinfo,
        ):
            await writer.close()
        assert excinfo.value.code == "INTERNAL_FORMAT_CLOSE"

    @pytest.mark.asyncio
    async def test_close_writes_statistics_and_marks_closed(self, tmp_path) -> None:
        writer = _StubWriter(path=str(tmp_path / "out"))
        writer.partitions = [0]
        writer.total_record_count = 4

        with patch(
            "application_sdk.storage.formats._upload_file", new_callable=AsyncMock
        ) as mock_upload:
            result = await writer.close()

        assert writer._is_closed is True
        # WriterResult subclasses TaskStatistics — inherited attributes.
        assert result.total_record_count == 4
        # Base Writer keeps the legacy inline-upload contract; no FileReference.
        assert result.files is None
        # Statistics file uploaded by base Writer (not overridden in _StubWriter).
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
# Per-instance state — `Reader._downloaded_files` must NOT be shared via a
# class-level mutable default.
# ---------------------------------------------------------------------------


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
