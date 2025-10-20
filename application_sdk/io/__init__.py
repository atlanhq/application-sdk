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
from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.common.types import DataframeType
from application_sdk.io._utils import estimate_dataframe_record_size, path_gen
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class Reader(ABC):
    """
    Abstract base class for reader data sources.
    """

    @abstractmethod
    def read_batches(
        self,
    ) -> Union[
        Iterator["pd.DataFrame"],
        AsyncIterator["pd.DataFrame"],
        Iterator["daft.DataFrame"],
        AsyncIterator["daft.DataFrame"],
    ]:
        """
        Get an iterator of batched pandas DataFrames.

        Returns:
            Iterator["pd.DataFrame"]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def read(self) -> Union["pd.DataFrame", "daft.DataFrame"]:
        """
        Get a single pandas or daft DataFrame.

        Returns:
            Union["pd.DataFrame", "daft.DataFrame"]: A pandas or daft DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
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
    to various destinations in different formats.

    Attributes:
        output_path (str): Path where the writer will be written.
        upload_file_prefix (str): Prefix for files when uploading to object store.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks the writer was split into.
        buffer_size (int): Size of the buffer to write data to.
        max_file_size_bytes (int): Maximum size of the file to write data to.
        current_buffer_size (int): Current size of the buffer to write data to.
        current_buffer_size_bytes (int): Current size of the buffer to write data to.
        partitions (List[int]): Partitions of the writer.
    """

    output_path: str
    output_prefix: str
    total_record_count: int
    chunk_count: int
    buffer_size: int
    max_file_size_bytes: int
    current_buffer_size: int
    current_buffer_size_bytes: int
    partitions: List[int]
    extension: str
    df_type: DataframeType

    async def write(self, dataframe: Union["pd.DataFrame", "daft.DataFrame"], **kwargs):
        """
        Method to write the pandas dataframe to an iceberg table
        """
        if self.df_type == DataframeType.pandas:
            await self._write_dataframe(dataframe, **kwargs)
        elif self.df_type == DataframeType.daft:
            await self._write_daft_dataframe(dataframe, **kwargs)
        else:
            raise ValueError(f"Unsupported df_type: {self.df_type}")

    async def write_batches(
        self,
        dataframe: Union[
            AsyncGenerator["pd.DataFrame", None],
            Generator["pd.DataFrame", None, None],
            AsyncGenerator["daft.DataFrame", None],
            Generator["daft.DataFrame", None, None],
        ],
    ):
        """
        Method to write the pandas dataframe to an iceberg table
        """
        if self.df_type == DataframeType.pandas:
            await self._write_batched_dataframe(dataframe)
        elif self.df_type == DataframeType.daft:
            await self._write_batched_daft_dataframe(dataframe)
        else:
            raise ValueError(f"Unsupported df_type: {self.df_type}")

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
                    output_file_name = f"{self.output_path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
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
                output_file_name = f"{self.output_path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
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

    @abstractmethod
    async def _write_daft_dataframe(self, dataframe: "daft.DataFrame", **kwargs):  # noqa: F821
        """Write a daft DataFrame to the output destination.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
            **kwargs: Additional parameters passed through from write().
        """
        pass

    async def get_statistics(
        self, typename: Optional[str] = None
    ) -> ActivityStatistics:
        """Returns statistics about the output.

        This method returns a ActivityStatistics object with total record count and chunk count.

        Args:
            typename (str): Type name of the entity e.g database, schema, table.

        Raises:
            ValidationError: If the statistics data is invalid
            Exception: If there's an error writing the statistics
        """
        try:
            statistics = await self.write_statistics()
            if not statistics:
                raise ValueError("No statistics data available")
            statistics = ActivityStatistics.model_validate(statistics)
            if typename:
                statistics.typename = typename
            return statistics
        except Exception as e:
            logger.error(f"Error getting statistics: {str(e)}")
            raise

    async def _upload_file(self, file_name: str):
        """Upload a file to the object store."""
        await ObjectStore.upload_file(
            source=file_name,
            destination=get_object_store_prefix(file_name),
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
                output_file_name = f"{self.output_path}/{path_gen(self.chunk_count, chunk_part, extension=self.extension)}"
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

    async def write_statistics(self) -> Optional[Dict[str, Any]]:
        """Write statistics about the output to a JSON file.

        This method writes statistics including total record count and chunk count
        to a JSON file and uploads it to the object store.

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

            # Write the statistics to a json file
            output_file_name = f"{self.output_path}/statistics.json.ignore"
            with open(output_file_name, "w") as f:
                f.write(orjson.dumps(statistics).decode("utf-8"))

            destination_file_path = get_object_store_prefix(output_file_name)
            # Push the file to the object store
            await ObjectStore.upload_file(
                source=output_file_name,
                destination=destination_file_path,
            )
            return statistics
        except Exception as e:
            logger.error(f"Error writing statistics: {str(e)}")


from application_sdk.io.iceberg import (  # noqa: E402
    IcebergTableReader,
    IcebergTableWriter,
)
from application_sdk.io.json import JsonFileReader, JsonFileWriter  # noqa: E402

# Import concrete reader and writer implementations
from application_sdk.io.parquet import (  # noqa: E402
    ParquetFileReader,
    ParquetFileWriter,
)

__all__ = [
    # Base classes
    "Reader",
    "Writer",
    "WriteMode",
    "DataframeType",
    # Reader and Writer implementations
    "ParquetFileReader",
    "ParquetFileWriter",
    "JsonFileReader",
    "JsonFileWriter",
    "IcebergTableReader",
    "IcebergTableWriter",
]
