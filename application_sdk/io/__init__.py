"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import gc
import inspect
import os
from abc import ABC, abstractmethod
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    AsyncIterator,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Union,
    cast,
)

import orjson
from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.types import DataframeType
from application_sdk.constants import ENABLE_ATLAN_UPLOAD, UPSTREAM_OBJECT_STORE_NAME
from application_sdk.io.utils import (
    estimate_dataframe_record_size,
    is_empty_dataframe,
    path_gen,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)
activity.logger = logger


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
    _is_closed: bool = False
    _downloaded_files: List[str] = []
    cleanup_on_close: bool = True

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
        import shutil

        for file_path in self._downloaded_files:
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to clean up temporary file {file_path}: {e}")

        self._downloaded_files.clear()

    @abstractmethod
    def read_batches(
        self,
    ) -> Union[
        Iterator["pd.DataFrame"],
        AsyncIterator["pd.DataFrame"],
        Iterator["daft.DataFrame"],
        AsyncIterator["daft.DataFrame"],
    ]:
        """Get an iterator of batched pandas DataFrames.

        Returns:
            Iterator["pd.DataFrame"]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
            ValueError: If the reader has been closed.
        """
        raise NotImplementedError

    @abstractmethod
    async def read(self) -> Union["pd.DataFrame", "daft.DataFrame"]:
        """Get a single pandas or daft DataFrame.

        Returns:
            Union["pd.DataFrame", "daft.DataFrame"]: A pandas or daft DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
            ValueError: If the reader has been closed.
        """
        raise NotImplementedError


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

            writer = JsonFileWriter(path="/data/output")
            await writer.write(dataframe)
            await writer.write({"key": "value"})  # Dict support
            stats = await writer.close()

        Using context manager (recommended)::

            async with JsonFileWriter(path="/data/output") as writer:
                await writer.write(dataframe)
            # close() called automatically
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
    partitions: List[int]
    extension: str
    dataframe_type: DataframeType
    _is_closed: bool = False
    _statistics: Optional[ActivityStatistics] = None

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
            "pd.DataFrame", "daft.DataFrame", Dict[str, Any], List[Dict[str, Any]]
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
        import pandas as pd

        # Already a pandas DataFrame - return as-is or convert to daft if needed
        if isinstance(data, pd.DataFrame):
            if self.dataframe_type == DataframeType.daft:
                try:
                    import daft

                    return daft.from_pandas(data)
                except ImportError:
                    raise TypeError(
                        "daft is not installed. Please install daft to use DataframeType.daft, "
                        "or use DataframeType.pandas instead."
                    )
            return data

        # Check for daft DataFrame
        try:
            import daft

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
                    import daft

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
                    raise TypeError(
                        "Dict and list inputs require daft to be installed when using DataframeType.daft. "
                        "Please install daft or use DataframeType.pandas instead."
                    )
            # For pandas dataframe_type, convert to pandas DataFrame
            return pd.DataFrame([data] if isinstance(data, dict) else data)

        raise TypeError(
            f"Unsupported data type: {type(data).__name__}. "
            "Expected DataFrame, dict, or list of dicts."
        )

    async def write(
        self,
        data: Union[
            "pd.DataFrame", "daft.DataFrame", Dict[str, Any], List[Dict[str, Any]]
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
            raise ValueError("Cannot write to a closed writer")

        # Convert to DataFrame if needed
        dataframe = self._convert_to_dataframe(data)

        if self.dataframe_type == DataframeType.pandas:
            await self._write_dataframe(dataframe, **kwargs)
        elif self.dataframe_type == DataframeType.daft:
            await self._write_daft_dataframe(dataframe, **kwargs)
        else:
            raise ValueError(f"Unsupported dataframe_type: {self.dataframe_type}")

    async def write_batches(
        self,
        dataframe: Union[
            AsyncGenerator["pd.DataFrame", None],
            Generator["pd.DataFrame", None, None],
            AsyncGenerator["daft.DataFrame", None],
            Generator["daft.DataFrame", None, None],
        ],
    ) -> None:
        """Write batched DataFrames to the output destination.

        Args:
            dataframe: Async or sync generator yielding DataFrames.

        Raises:
            ValueError: If the writer has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            raise ValueError("Cannot write to a closed writer")

        if self.dataframe_type == DataframeType.pandas:
            await self._write_batched_dataframe(dataframe)
        elif self.dataframe_type == DataframeType.daft:
            await self._write_batched_daft_dataframe(dataframe)
        else:
            raise ValueError(f"Unsupported dataframe_type: {self.dataframe_type}")

    async def _write_batched_dataframe(
        self,
        batched_dataframe: Union[
            AsyncGenerator["pd.DataFrame", None], Generator["pd.DataFrame", None, None]
        ],
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
            logger.error(f"Error writing batched dataframe: {str(e)}")
            raise

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

                if (
                    self.current_buffer_size_bytes + chunk_size_bytes
                    > self.max_file_size_bytes
                ):
                    output_file_name = f"{self.path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
                    if os.path.exists(output_file_name):
                        await self._upload_file(output_file_name)
                        self.chunk_part += 1

                self.current_buffer_size += len(chunk)
                self.current_buffer_size_bytes += chunk_size_bytes * len(chunk)
                await self._flush_buffer(chunk, self.chunk_part)

                del chunk
                gc.collect()

            if self.current_buffer_size_bytes > 0:
                # Finally upload the final file to the object store
                output_file_name = f"{self.path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
                if os.path.exists(output_file_name):
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
                    "error": str(e),
                },
                description="Number of errors while writing to files",
            )
            logger.error(f"Error writing pandas dataframe to files: {str(e)}")
            raise

    async def _write_batched_daft_dataframe(
        self,
        batched_dataframe: Union[
            AsyncGenerator["daft.DataFrame", None],  # noqa: F821
            Generator["daft.DataFrame", None, None],  # noqa: F821
        ],
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
                )  # noqa: F821
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self._write_daft_dataframe(dataframe)
        except Exception as e:
            logger.error(f"Error writing batched daft dataframe: {str(e)}")
            raise

    @abstractmethod
    async def _write_daft_dataframe(self, dataframe: "daft.DataFrame", **kwargs):  # noqa: F821
        """Write a daft DataFrame to the output destination.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
            **kwargs: Additional parameters passed through from write().
        """
        pass

    @property
    def statistics(self) -> ActivityStatistics:
        """Get current statistics without closing the writer.

        Returns:
            ActivityStatistics: Current statistics (record count, chunk count, partitions).

        Note:
            This returns the current state. For final statistics after all
            writes complete, use close() instead.
        """
        return ActivityStatistics(
            total_record_count=self.total_record_count,
            chunk_count=len(self.partitions),
            partitions=self.partitions,
        )

    async def _finalize(self) -> None:
        """Finalize the writer before closing.

        Override this method in subclasses to perform any final flush operations,
        upload remaining files, etc. This is called by close() before writing statistics.
        """
        pass

    async def close(self) -> ActivityStatistics:
        """Close the writer, flush buffers, upload files, and return statistics.

        This method finalizes all pending writes, uploads any remaining files to
        the object store, writes statistics, and marks the writer as closed.
        Calling close() multiple times is safe (subsequent calls are no-ops).

        The typename for statistics is automatically taken from `self.typename`
        if it was set during initialization.

        Returns:
            ActivityStatistics: Final statistics including total_record_count,
                chunk_count, and partitions.

        Raises:
            ValueError: If statistics data is invalid.
            Exception: If there's an error during finalization or writing statistics.

        Example:
            ```python
            writer = JsonFileWriter(path="/data/output", typename="table")
            await writer.write(dataframe)
            stats = await writer.close()
            print(f"Wrote {stats.total_record_count} records")
            ```
        """
        if self._is_closed:
            if self._statistics:
                return self._statistics
            return self.statistics

        try:
            # Allow subclasses to perform final flush/upload operations
            await self._finalize()

            # Use self.typename if available
            typename = getattr(self, "typename", None)

            # Write statistics to file and object store
            statistics_dict = await self._write_statistics(typename)
            if not statistics_dict:
                raise ValueError("No statistics data available")

            self._statistics = ActivityStatistics.model_validate(statistics_dict)
            if typename:
                self._statistics.typename = typename

            self._is_closed = True
            return self._statistics

        except Exception as e:
            logger.error(f"Error closing writer: {str(e)}")
            raise

    async def _upload_file(self, file_name: str):
        """Upload a file to the object store."""
        # Get retain_local_copy from the writer instance, defaulting to False
        retain_local = getattr(self, "retain_local_copy", False)

        if ENABLE_ATLAN_UPLOAD:
            await ObjectStore.upload_file(
                source=file_name,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
                retain_local_copy=True,  # Always retain for the second upload to deployment store
                destination=get_object_store_prefix(file_name),
            )
        await ObjectStore.upload_file(
            source=file_name,
            destination=get_object_store_prefix(file_name),
            retain_local_copy=retain_local,  # Respect the writer's retain_local_copy setting
        )

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
                    labels={"type": "output"},
                    description="Number of chunks written to files",
                )

        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "output", "error": str(e)},
                description="Number of errors while writing to files",
            )
            logger.error(f"Error flushing buffer to files: {str(e)}")
            raise e

    async def _write_statistics(
        self, typename: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
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
                # If accessing chunk_start fails, fallback to default filename
                pass

            # Write the statistics dictionary to the JSON file
            with open(output_file_name, "wb") as f:
                f.write(orjson.dumps(statistics))

            destination_file_path = get_object_store_prefix(output_file_name)
            # Push the file to the object store
            await ObjectStore.upload_file(
                source=output_file_name,
                destination=destination_file_path,
            )

            return statistics
        except Exception as e:
            logger.error(f"Error writing statistics: {str(e)}")
            raise
