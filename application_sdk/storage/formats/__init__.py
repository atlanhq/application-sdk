"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import gc
import inspect
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Union, cast

import orjson

from application_sdk.common.models import TaskStatistics
from application_sdk.common.types import DataframeType
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType
from application_sdk.storage.formats.utils import (
    estimate_dataframe_record_size,
    is_empty_dataframe,
    path_gen,
)
from application_sdk.storage.ops import upload_file as _upload_file


@dataclass
class WriterResult(TaskStatistics):
    """Outcome of a Writer.close() call.

    Subclasses ``TaskStatistics`` so existing callers that read
    ``result.total_record_count``, ``result.chunk_count``, ``result.partitions``,
    ``result.typename`` keep working unchanged via inheritance.

    Adds one new field — ``files`` — for callers who opted into the deferred-
    upload contract via ``defer_uploads=True`` on the writer constructor.
    When deferred uploads are off (the default), ``files`` is ``None`` because
    files have already been uploaded inline and surfacing a ``FileReference``
    would risk a double-upload through the activity interceptor.

    Apps that want SHA-256 dedup, integrity verification, and parallel
    transfers via the ``FileReference`` boundary set ``defer_uploads=True``
    and read ``result.files`` here. Apps that don't care can ignore this
    field entirely — their existing code paths are unaffected.
    """

    files: FileReference | None = None


logger = get_logger(__name__)


if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class Reader(ABC):
    """Abstract base class for reader data sources.

    This class defines the interface for reader handlers that can read data
    from various sources in different formats. Follows Python's file I/O
    pattern with read/close semantics and supports context managers.

    Attributes:
        path (str): Path where the reader will read from.
        _is_closed (bool): Whether the reader has been closed.
        _downloaded_files (List[str]): List of downloaded temporary files to clean up.
        cleanup_on_close (bool): Whether to clean up downloaded temp files on close.

    Example:
        Using close() explicitly::

            reader = ParquetFileReader(path="/data/input")
            df = await reader.read()
            await reader.close()  # Cleans up any downloaded temp files

        Using context manager (recommended)::

            async with ParquetFileReader(path="/data/input") as reader:
                df = await reader.read()
            # close() called automatically

        Reading in batches with context manager::

            async with JsonFileReader(path="/data/input") as reader:
                async for batch in reader.read_batches():
                    process(batch)
            # close() called automatically
    """

    path: str
    _is_closed: bool
    _downloaded_files: list[str]
    cleanup_on_close: bool = True

    def __init__(self) -> None:
        """Initialize per-instance mutable state.

        Subclasses that override ``__init__`` should call ``super().__init__()``
        to ensure ``_downloaded_files`` and ``_is_closed`` are not shared across
        instances via class-level mutable defaults.
        """
        self._downloaded_files: list[str] = []
        self._is_closed: bool = False

    async def __aenter__(self) -> "Reader":
        """Enter the async context manager.

        Returns:
            Reader: The reader instance.
        """
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the async context manager, closing the reader.

        Args:
            exc_type: Exception type if an exception was raised.
            exc_val: Exception value if an exception was raised.
            exc_tb: Exception traceback if an exception was raised.
        """
        await self.close()

    async def close(self) -> None:
        """Close the reader and clean up any downloaded temporary files.

        This method cleans up any temporary files that were downloaded from
        the object store during read operations. Calling close() multiple
        times is safe (subsequent calls are no-ops).

        Note:
            Set ``cleanup_on_close=False`` during initialization to retain
            downloaded files after closing.

        Example::

            reader = ParquetFileReader(path="/data/input")
            df = await reader.read()
            await reader.close()  # Cleans up temp files
        """
        if self._is_closed:
            return

        if self.cleanup_on_close and self._downloaded_files:
            await self._cleanup_downloaded_files()

        self._is_closed = True

    async def _cleanup_downloaded_files(self) -> None:
        """Clean up downloaded temporary files.

        Override this method in subclasses for custom cleanup behavior.
        """
        import shutil  # noqa: PLC0415 — stdlib shutil; lazy use only

        for file_path in self._downloaded_files:
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path, ignore_errors=True)
            except Exception:
                logger.warning(
                    "Failed to clean up temporary file: %s",
                    file_path,
                    exc_info=True,
                )

        self._downloaded_files.clear()

    @abstractmethod
    def read_batches(
        self,
    ) -> (
        Iterator["pd.DataFrame"]
        | AsyncIterator["pd.DataFrame"]
        | Iterator["daft.DataFrame"]
        | AsyncIterator["daft.DataFrame"]
    ):
        """Get an iterator of batched pandas DataFrames.

        Returns:
            Iterator["pd.DataFrame"]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
            ValueError: If the reader has been closed.
        """
        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            AbstractFormatReaderError,
        )

        raise AbstractFormatReaderError()

    @abstractmethod
    async def read(self) -> Union["pd.DataFrame", "daft.DataFrame"]:
        """Get a single pandas or daft DataFrame.

        Returns:
            Union["pd.DataFrame", "daft.DataFrame"]: A pandas or daft DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
            ValueError: If the reader has been closed.
        """
        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            AbstractFormatReaderError,
        )

        raise AbstractFormatReaderError()


class WriteMode(Enum):
    """Enumeration of write modes for output operations."""

    APPEND = "append"
    OVERWRITE = "overwrite"
    OVERWRITE_PARTITIONS = "overwrite-partitions"


class Writer(ABC):
    """Abstract base class for writer handlers.

    This class defines the interface for writer handlers that can write data
    to various destinations in different formats. Follows Python's file I/O
    pattern with open/write/close semantics and supports context managers.

    Attributes:
        path (str): Path where the writer will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks the writer was split into.
        buffer_size (int): Size of the buffer to write data to.
        max_file_size_bytes (int): Maximum size of the file to write data to.
        current_buffer_size (int): Current size of the buffer to write data to.
        current_buffer_size_bytes (int): Current size of the buffer to write data to.
        partitions (List[int]): Partitions of the writer.

    Example:
        Using close() explicitly::

            writer = ParquetFileWriter(path="/data/output", typename="users")
            await writer.write(dataframe)
            result = await writer.close()
            # result.statistics → TaskStatistics
            # result.files      → ephemeral FileReference for the output dir

        Using context manager (recommended)::

            async with ParquetFileWriter(path=base, typename="users") as w:
                await w.write(dataframe)
            # close() called automatically; final result retrievable via
            # w.last_result if needed.
    """

    path: str
    output_prefix: str
    total_record_count: int
    chunk_count: int
    chunk_part: int
    buffer_size: int
    max_file_size_bytes: int
    current_buffer_size: int
    current_buffer_size_bytes: int
    partitions: list[int]
    extension: str
    dataframe_type: DataframeType
    _is_closed: bool = False
    _statistics: TaskStatistics | None = None
    _result: "WriterResult | None" = None

    async def __aenter__(self) -> "Writer":
        """Enter the async context manager.

        Returns:
            Writer: The writer instance.
        """
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the async context manager, closing the writer.

        Args:
            exc_type: Exception type if an exception was raised.
            exc_val: Exception value if an exception was raised.
            exc_tb: Exception traceback if an exception was raised.
        """
        await self.close()

    def _convert_to_dataframe(
        self,
        data: Union[
            "pd.DataFrame", "daft.DataFrame", dict[str, Any], list[dict[str, Any]]
        ],
    ) -> Union["pd.DataFrame", "daft.DataFrame"]:
        """Convert input data to a DataFrame if needed.

        Args:
            data: Input data - can be a DataFrame, dict, or list of dicts.

        Returns:
            A pandas or daft DataFrame depending on self.dataframe_type.

        Raises:
            TypeError: If data type is not supported or if dict/list input is used with daft when daft is not available.
        """
        import pandas as pd  # noqa: PLC0415 — optional dep: pandas

        # Already a pandas DataFrame - return as-is or convert to daft if needed
        if isinstance(data, pd.DataFrame):
            if self.dataframe_type == DataframeType.daft:
                try:
                    import daft  # noqa: PLC0415 — optional dep: daft

                    return daft.from_pandas(data)
                except ImportError:
                    from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                        DaftNotInstalledError,
                    )

                    raise DaftNotInstalledError()
            return data

        # Check for daft DataFrame
        try:
            import daft  # noqa: PLC0415 — optional dep: daft

            if isinstance(data, daft.DataFrame):
                return data
        except ImportError:
            pass

        # Convert dict or list of dicts to DataFrame
        if isinstance(data, dict) or (
            isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict)
        ):
            # For daft dataframe_type, convert to daft DataFrame directly
            if self.dataframe_type == DataframeType.daft:
                try:
                    import daft  # noqa: PLC0415 — optional dep: daft

                    # Convert to columnar format for daft.from_pydict()
                    if isinstance(data, dict):
                        # Single dict: {"col1": "val1", "col2": "val2"} -> {"col1": ["val1"], "col2": ["val2"]}
                        columnar_data = {k: [v] for k, v in data.items()}
                    else:
                        # List of dicts: [{"col1": "v1"}, {"col1": "v2"}] -> {"col1": ["v1", "v2"]}
                        columnar_data = {}
                        for record in data:
                            for key, value in record.items():
                                if key not in columnar_data:
                                    columnar_data[key] = []
                                columnar_data[key].append(value)
                    return daft.from_pydict(columnar_data)
                except ImportError:
                    from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                        DaftNotInstalledError,
                    )

                    raise DaftNotInstalledError()
            # For pandas dataframe_type, convert to pandas DataFrame
            return pd.DataFrame([data] if isinstance(data, dict) else data)

        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            UnsupportedDataTypeError,
        )

        raise UnsupportedDataTypeError(observed_type=type(data).__name__)

    async def write(
        self,
        data: Union[
            "pd.DataFrame", "daft.DataFrame", dict[str, Any], list[dict[str, Any]]
        ],
        **kwargs: Any,
    ) -> None:
        """Write data to the output destination.

        Supports writing DataFrames, dicts (converted to single-row DataFrame),
        or lists of dicts (converted to multi-row DataFrame).

        Args:
            data: Data to write - DataFrame, dict, or list of dicts.
            **kwargs: Additional parameters passed to the underlying write method.

        Raises:
            ValueError: If the writer has been closed or dataframe_type is unsupported.
            TypeError: If data type is not supported.
        """
        if self._is_closed:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                WriterClosedError,
            )

            raise WriterClosedError()

        # Convert to DataFrame if needed
        dataframe = self._convert_to_dataframe(data)

        if self.dataframe_type == DataframeType.pandas:
            await self._write_dataframe(dataframe, **kwargs)
        elif self.dataframe_type == DataframeType.daft:
            await self._write_daft_dataframe(dataframe, **kwargs)
        else:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                UnsupportedDataframeTypeError,
            )

            raise UnsupportedDataframeTypeError(observed_type=str(self.dataframe_type))

    async def write_batches(
        self,
        dataframe: AsyncGenerator["pd.DataFrame", None]
        | Generator["pd.DataFrame", None, None]
        | AsyncGenerator["daft.DataFrame", None]
        | Generator["daft.DataFrame", None, None],
    ) -> None:
        """Write batched DataFrames to the output destination.

        Args:
            dataframe: Async or sync generator yielding DataFrames.

        Raises:
            ValueError: If the writer has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                WriterClosedError,
            )

            raise WriterClosedError()

        if self.dataframe_type == DataframeType.pandas:
            await self._write_batched_dataframe(dataframe)
        elif self.dataframe_type == DataframeType.daft:
            await self._write_batched_daft_dataframe(dataframe)
        else:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                UnsupportedDataframeTypeError,
            )

            raise UnsupportedDataframeTypeError(observed_type=str(self.dataframe_type))

    async def _write_batched_dataframe(
        self,
        batched_dataframe: AsyncGenerator["pd.DataFrame", None]
        | Generator["pd.DataFrame", None, None],
    ):
        """Write a batched pandas DataFrame to Output.

        This method writes the DataFrame to Output provided, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self._write_dataframe(dataframe)
            else:
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["pd.DataFrame", None, None], batched_dataframe
                )
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self._write_dataframe(dataframe)
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatWriteError,
            )

            raise FormatWriteError(cause=e) from e

    async def _write_dataframe(self, dataframe: "pd.DataFrame", **kwargs):
        """Write a pandas DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
            **kwargs: Additional parameters (currently unused for pandas DataFrames).
        """
        try:
            if self.chunk_start is None:
                self.chunk_part = 0
            if len(dataframe) == 0:
                return

            chunk_size_bytes = estimate_dataframe_record_size(dataframe, self.extension)

            for i in range(0, len(dataframe), self.buffer_size):
                chunk = dataframe[i : i + self.buffer_size]

                # Only upload accumulated data if there is any — guards against
                # the first chunk being larger than max_file_size_bytes where
                # no prior _flush_buffer call has written the file yet.
                if (
                    self.current_buffer_size_bytes + chunk_size_bytes
                    > self.max_file_size_bytes
                    and self.current_buffer_size_bytes > 0
                ):
                    output_file_name = f"{self.path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
                    await self._upload_file(output_file_name)
                    self.chunk_part += 1

                self.current_buffer_size += len(chunk)
                self.current_buffer_size_bytes += chunk_size_bytes * len(chunk)
                await self._flush_buffer(chunk, self.chunk_part)

                del chunk
                gc.collect()

            if self.current_buffer_size_bytes > 0:
                # Finally upload the final file to the object store.
                # _flush_buffer already wrote the file; no existence check needed.
                output_file_name = f"{self.path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
                await self._upload_file(output_file_name)
                self.chunk_part += 1

            # Record metrics for successful write
            self.metrics.record_metric(
                name="write_records",
                value=len(dataframe),
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": WriteMode.APPEND.value},
                description="Number of records written to files from pandas DataFrame",
            )

            # Record chunk metrics
            self.metrics.record_metric(
                name="chunks_written",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": WriteMode.APPEND.value},
                description="Number of chunks written to files",
            )

            # If chunk_start is set we don't want to increment the chunk_count
            # Since it should only increment the chunk_part in this case
            if self.chunk_start is None:
                self.chunk_count += 1
            self.partitions.append(self.chunk_part)
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={
                    "type": "pandas",
                    "mode": WriteMode.APPEND.value,
                    "error_type": type(e).__name__,
                },
                description="Number of errors while writing to files",
            )
            raise

    async def _write_batched_daft_dataframe(
        self,
        batched_dataframe: AsyncGenerator["daft.DataFrame", None]
        | Generator["daft.DataFrame", None, None],
    ):
        """Write a batched daft DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self._write_daft_dataframe(dataframe)
            else:
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["daft.DataFrame", None, None], batched_dataframe
                )
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self._write_daft_dataframe(dataframe)
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatWriteError,
            )

            raise FormatWriteError(cause=e) from e

    @abstractmethod
    async def _write_daft_dataframe(self, dataframe: "daft.DataFrame", **kwargs):
        """Write a daft DataFrame to the output destination.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
            **kwargs: Additional parameters passed through from write().
        """

    @property
    def last_result(self) -> "WriterResult | None":
        """Return the result of the most recent close(), or None if not closed yet.

        Useful when calling close() implicitly via ``async with``: the
        context manager discards close()'s return value, so read it here
        afterwards::

            async with ParquetFileWriter(path=base, typename="t") as w:
                await w.write(df)
            result = w.last_result  # WriterResult
        """
        return self._result

    @property
    def statistics(self) -> TaskStatistics:
        """Get current statistics without closing the writer.

        Returns:
            TaskStatistics: Current statistics (record count, chunk count, partitions).

        Note:
            This returns the current state. For final statistics after all
            writes complete, use close() instead.
        """
        return TaskStatistics(
            total_record_count=self.total_record_count,
            chunk_count=len(self.partitions),
            partitions=self.partitions,
        )

    async def _finalize(self) -> None:
        """Finalize the writer before closing.

        Override this method in subclasses to perform any final flush operations,
        upload remaining files, etc. This is called by close() before writing statistics.
        """

    async def close(self) -> WriterResult:
        """Close the writer, flush buffers, and return statistics + file reference.

        Finalizes all pending writes, writes the statistics sidecar, and marks
        the writer as closed. Calling close() multiple times is safe — subsequent
        calls return the cached :class:`WriterResult`.

        The returned :class:`WriterResult` carries an ephemeral
        :class:`FileReference` pointing at the writer-owned output directory
        (``self.path``). When that ``FileReference`` is placed on an activity's
        typed Output, the Temporal interceptor's ``persist_file_refs`` uploads
        it transparently with SHA-256 sidecars — callers do not need to call
        ``persist_file_reference`` themselves.

        Returns:
            WriterResult: ``statistics`` (record/chunk counts) and ``files``
                (ephemeral ``FileReference`` to the output directory).

        Raises:
            ValueError: If statistics data is invalid.
            Exception: If there's an error during finalization or writing statistics.

        Example:
            ```python
            async with ParquetFileWriter(path=base, typename="table") as w:
                await w.write(df)
            result = await w.close()
            return MyOutput(statistics=result.statistics, data=result.files)
            ```
        """
        if self._is_closed:
            if self._result is not None:
                return self._result
            # Idempotent fallback: re-derive when called more than once on an
            # already-closed instance with no cached result (defensive — should
            # not happen in normal flow).
            base = self._statistics or self.statistics
            return WriterResult(
                total_record_count=base.total_record_count,
                chunk_count=base.chunk_count,
                partitions=base.partitions,
                typename=base.typename,
                files=self._build_file_reference(),
            )

        try:
            # Allow subclasses to perform final flush/upload operations
            await self._finalize()

            # Use self.typename if available
            typename = getattr(self, "typename", None)

            # Write statistics to file and object store
            statistics_dict = await self._write_statistics(typename)
            if not statistics_dict:
                from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                    MissingStatisticsError,
                )

                raise MissingStatisticsError()

            self._statistics = TaskStatistics(**statistics_dict)
            if typename:
                self._statistics.typename = typename

            self._is_closed = True
            self._result = WriterResult(
                total_record_count=self._statistics.total_record_count,
                chunk_count=self._statistics.chunk_count,
                partitions=self._statistics.partitions,
                typename=self._statistics.typename,
                files=self._build_file_reference(),
            )
            return self._result

        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatCloseError,
            )

            raise FormatCloseError(cause=e) from e

    def _build_file_reference(self) -> "FileReference | None":
        """Return an ephemeral FileReference for the writer's output directory.

        Only populated when the subclass opts into deferred uploads (e.g.
        ``ParquetFileWriter(defer_uploads=True)``). For the default
        inline-upload path, returns ``None`` so the activity interceptor
        does not double-upload files that are already in the object store.

        Subclasses that defer uploads override this to return
        ``FileReference.from_local(self.path)``.
        """
        return None

    async def _upload_file(self, file_name: str):
        """Upload a file to the object store."""
        retain_local = getattr(self, "retain_local_copy", False)
        await _upload_file(file_name, file_name, retain_local_copy=retain_local)
        self.current_buffer_size_bytes = 0

    async def _flush_buffer(self, chunk: "pd.DataFrame", chunk_part: int):
        """Flush the current buffer to a JSON file.

        This method combines all DataFrames in the buffer, writes them to a JSON file,
        and uploads the file to the object store.

        Note:
            If the buffer is empty or has no records, the method returns without writing.
        """
        try:
            if not is_empty_dataframe(chunk):
                self.total_record_count += len(chunk)
                output_file_name = f"{self.path}/{path_gen(self.chunk_count, chunk_part, extension=self.extension)}"
                await self._write_chunk(chunk, output_file_name)

                self.current_buffer_size = 0

                # Record chunk metrics
                self.metrics.record_metric(
                    name="chunks_written",
                    value=1,
                    metric_type=MetricType.COUNTER,
                    labels={"type": "output", "mode": WriteMode.APPEND.value},
                    description="Number of chunks written to files",
                )

        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={
                    "type": "output",
                    "mode": WriteMode.APPEND.value,
                    "error_type": type(e).__name__,
                },
                description="Number of errors while writing to files",
            )
            raise

    async def _write_statistics(
        self, typename: str | None = None
    ) -> dict[str, Any] | None:
        """Write statistics about the output to a JSON file.

        Internal method called by close() to persist statistics.

        Args:
            typename (str, optional): Type name for organizing statistics.

        Returns:
            Dict containing statistics data.

        Raises:
            Exception: If there's an error writing or uploading the statistics.
        """
        try:
            # prepare the statistics
            statistics = {
                "total_record_count": self.total_record_count,
                "chunk_count": len(self.partitions),
                "partitions": self.partitions,
            }

            # Ensure typename is included in the statistics payload (if provided)
            if typename:
                statistics["typename"] = typename

            # Write the statistics to a json file inside a dedicated statistics/ folder
            statistics_dir = os.path.join(self.path, "statistics")
            os.makedirs(statistics_dir, exist_ok=True)
            output_file_name = os.path.join(statistics_dir, "statistics.json.ignore")
            # If chunk_start is provided, include it in the statistics filename
            try:
                cs = getattr(self, "chunk_start", None)
                if cs is not None:
                    output_file_name = os.path.join(
                        statistics_dir, f"statistics-chunk-{cs}.json.ignore"
                    )
            except Exception:
                logger.warning(
                    "Failed to access chunk_start for statistics filename, using default",
                    exc_info=True,
                )

            # Write the statistics dictionary to the JSON file
            with open(output_file_name, "wb") as f:
                f.write(orjson.dumps(statistics))

            # Push the file to the object store (key = local path for consistency).
            # ParquetFileWriter with defer_uploads=True overrides _upload_file
            # to a no-op so the statistics sidecar travels via close()'s
            # returned FileReference instead of inline.
            await self._upload_file(output_file_name)

            return statistics
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatStatisticsWriteError,
            )

            raise FormatStatisticsWriteError(cause=e) from e
