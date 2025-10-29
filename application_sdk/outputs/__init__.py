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
    Dict,
    Generator,
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
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class WriteMode(Enum):
    """Enumeration of write modes for output operations."""

    APPEND = "append"
    OVERWRITE = "overwrite"
    OVERWRITE_PARTITIONS = "overwrite-partitions"


class Output(ABC):
    """Abstract base class for output handlers.

    This class defines the interface for output handlers that can write data
    to various destinations in different formats.

    Attributes:
        output_path (str): Path where the output will be written.
        upload_file_prefix (str): Prefix for files when uploading to object store.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks the output was split into.
    """

    output_path: str
    output_prefix: str
    total_record_count: int
    chunk_count: int
    chunk_part: int
    buffer_size: int
    max_file_size_bytes: int
    current_buffer_size: int
    current_buffer_size_bytes: int
    partitions: List[int]

    def estimate_dataframe_record_size(self, dataframe: "pd.DataFrame") -> int:
        """Estimate File size of a DataFrame by sampling a few records."""
        if len(dataframe) == 0:
            return 0

        # Sample up to 10 records to estimate average size
        sample_size = min(10, len(dataframe))
        sample = dataframe.head(sample_size)
        file_type = type(self).__name__.lower().replace("output", "")
        compression_factor = 1
        if file_type == "json":
            sample_file = sample.to_json(orient="records", lines=True)
        else:
            sample_file = sample.to_parquet(index=False, compression="snappy")
            compression_factor = 0.01
        if sample_file is not None:
            avg_record_size = len(sample_file) / sample_size * compression_factor
            return int(avg_record_size)

        return 0

    def path_gen(
        self,
        chunk_count: Optional[int] = None,
        chunk_part: int = 0,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
    ) -> str:
        """Generate a file path for a chunk.

        Args:
            chunk_start (Optional[int]): Starting index of the chunk, or None for single chunk.
            chunk_count (int): Total number of chunks.
            start_marker (Optional[str]): Start marker for query extraction.
            end_marker (Optional[str]): End marker for query extraction.

        Returns:
            str: Generated file path for the chunk.
        """
        # For Query Extraction - use start and end markers without chunk count
        if start_marker and end_marker:
            return f"{start_marker}_{end_marker}{self._EXTENSION}"

        # For regular chunking - include chunk count
        if chunk_count is None:
            return f"{str(chunk_part)}{self._EXTENSION}"
        else:
            return f"chunk-{str(chunk_count)}-part{str(chunk_part)}{self._EXTENSION}"

    def process_null_fields(
        self,
        obj: Any,
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> Any:
        """
        By default the method removes null values from dictionaries and lists.
        Except for the fields specified in preserve_fields.
        And fields in null_to_empty_dict_fields are replaced with empty dict if null.

        Args:
            obj: The object to clean (dict, list, or other value)
            preserve_fields: Optional list of field names that should be preserved even if they contain null values
            null_to_empty_dict_fields: Optional list of field names that should be replaced with empty dict if null

        Returns:
            The cleaned object with null values removed
        """
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                # Handle null fields that should be converted to empty dicts
                if k in (null_to_empty_dict_fields or []) and v is None:
                    result[k] = {}
                    continue

                # Process the value recursively
                processed_value = self.process_null_fields(
                    v, preserve_fields, null_to_empty_dict_fields
                )

                # Keep the field if it's in preserve_fields or has a non-None processed value
                if k in (preserve_fields or []) or processed_value is not None:
                    result[k] = processed_value

            return result
        return obj

    async def write_batched_dataframe(
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
                        await self.write_dataframe(dataframe)
            else:
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["pd.DataFrame", None, None], batched_dataframe
                )
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
        except Exception as e:
            logger.error(f"Error writing batched dataframe: {str(e)}")
            raise

    async def write_dataframe(self, dataframe: "pd.DataFrame"):
        """Write a pandas DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
        """
        try:
            if self.chunk_start is None:
                self.chunk_part = 0
            if len(dataframe) == 0:
                return

            chunk_size_bytes = self.estimate_dataframe_record_size(dataframe)

            for i in range(0, len(dataframe), self.buffer_size):
                chunk = dataframe[i : i + self.buffer_size]

                if (
                    self.current_buffer_size_bytes + chunk_size_bytes
                    > self.max_file_size_bytes
                ):
                    output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_count, self.chunk_part)}"
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
                output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_count, self.chunk_part)}"
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

    async def write_batched_daft_dataframe(
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
                        await self.write_daft_dataframe(dataframe)
            else:
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["daft.DataFrame", None, None], batched_dataframe
                )  # noqa: F821
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
        except Exception as e:
            logger.error(f"Error writing batched daft dataframe: {str(e)}")

    @abstractmethod
    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to the output destination.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
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
                output_file_name = (
                    f"{self.output_path}/{self.path_gen(self.chunk_count, chunk_part)}"
                )
                await self.write_chunk(chunk, output_file_name)

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
