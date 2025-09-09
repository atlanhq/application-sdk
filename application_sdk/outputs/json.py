import gc
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

import orjson
from temporalio import activity

from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.outputs import Output

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


def path_gen(chunk_start: int | None, chunk_count: int) -> str:
    """Generate a file path for a chunk.

    Args:
        chunk_start (int | None): Starting index of the chunk, or None for single chunk.
        chunk_count (int): Total number of chunks.

    Returns:
        str: Generated file path for the chunk.
    """
    if chunk_start is None:
        return f"{str(chunk_count)}.json"
    else:
        return f"chunk-{chunk_start}-part{chunk_count}.json"


def convert_datetime_to_epoch(data: Any) -> Any:
    """Convert datetime objects to epoch timestamps in milliseconds.

    Args:
        data: The data to convert

    Returns:
        The converted data with datetime fields as epoch timestamps
    """
    if isinstance(data, datetime):
        return int(data.timestamp() * 1000)
    elif isinstance(data, dict):
        return {k: convert_datetime_to_epoch(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_datetime_to_epoch(item) for item in data]
    return data


class JsonOutput(Output):
    """Output handler for writing data to JSON files.

    This class provides functionality for writing data to JSON files with support
    for chunking large datasets, buffering, and automatic file path generation.
    It can handle both pandas and daft DataFrames as input.

    The output can be written to local files and optionally uploaded to an object
    store. Files are named using a configurable path generation scheme that
    includes chunk numbers for split files.

    Attributes:
        output_path (Optional[str]): Base path where JSON files will be written.
        output_suffix (str): Suffix added to file paths when uploading to object store.
        output_prefix (Optional[str]): Prefix for output files and object store paths.
        typename (Optional[str]): Type identifier for the data being written.
        chunk_start (Optional[int]): Starting index for chunk numbering.
        buffer_size (int): Size of the write buffer in bytes.
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks written.
        buffer (List[Union[pd.DataFrame, daft.DataFrame]]): Buffer for accumulating
            data before writing.
    """

    def __init__(
        self,
        output_suffix: str,
        output_path: Optional[str] = None,
        output_prefix: Optional[str] = None,
        typename: Optional[str] = None,
        chunk_start: Optional[int] = None,
        buffer_size: int = 5000,
        chunk_size: Optional[int] = 5000,
        total_record_count: int = 0,
        chunk_count: int = 0,
        path_gen: Callable[[int | None, int], str] = path_gen,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JSON output handler.

        Args:
            output_path (str): Path where JSON files will be written.
            output_suffix (str): Prefix for files when uploading to object store.
            output_prefix (Optional[str], optional): Prefix for files where the files will be written and uploaded.
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            buffer_size (int, optional): Size of the buffer in bytes.
                Defaults to 10MB (1024 * 1024 * 10).
            chunk_size (Optional[int], optional): Maximum number of records per chunk. If None, uses config value.
                Defaults to None.
            total_record_count (int, optional): Initial total record count.
                Defaults to 0.
            chunk_count (int, optional): Initial chunk count.
                Defaults to 0.
            path_gen (Callable, optional): Function to generate file paths.
                Defaults to path_gen function.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.chunk_start = chunk_start
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.buffer_size = buffer_size
        self.chunk_size = chunk_size or 5000
        self.buffer: List[Union["pd.DataFrame", "daft.DataFrame"]] = []  # noqa: F821
        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0  # Track estimated buffer size in bytes
        self.max_file_size_bytes = int(
            DAPR_MAX_GRPC_MESSAGE_LENGTH * 0.9
        )  # 90% of DAPR limit as safety buffer
        self.path_gen = path_gen
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.metrics = get_metrics()

        if not self.output_path:
            raise ValueError("output_path is required")

        self.output_path = os.path.join(self.output_path, output_suffix)
        if typename:
            self.output_path = os.path.join(self.output_path, typename)
        os.makedirs(self.output_path, exist_ok=True)

        # For Query Extraction
        if self.start_marker and self.end_marker:
            self.path_gen = (
                lambda chunk_start,
                chunk_count: f"{self.start_marker}_{self.end_marker}.json"
            )

    async def write_dataframe(self, dataframe: "pd.DataFrame"):
        """Write a pandas DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            chunk_part = 0
            if len(dataframe) == 0:
                return

            for i in range(0, len(dataframe), self.buffer_size):
                chunk = dataframe[i : i + self.buffer_size]
                # Estimate size of this chunk
                chunk_size_bytes = self.estimate_dataframe_file_size(chunk, "json")

                # Check if adding this chunk would exceed size limit
                if (
                    self.current_buffer_size_bytes + chunk_size_bytes
                    > self.max_file_size_bytes
                ):
                    output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_count, chunk_part)}"
                    await self._upload_file(output_file_name)
                    chunk_part += 1

                self.current_buffer_size += len(chunk)
                self.current_buffer_size_bytes += chunk_size_bytes
                await self._flush_buffer(chunk, chunk_part)

                del chunk
                gc.collect()

            if self.current_buffer_size > 0:
                output_file_name = (
                    f"{self.output_path}/{self.path_gen(self.chunk_count, chunk_part)}"
                )
                await self._upload_file(output_file_name)

            # Record metrics for successful write
            self.metrics.record_metric(
                name="json_write_records",
                value=len(dataframe),
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas"},
                description="Number of records written to JSON files from pandas DataFrame",
            )

            # Record chunk metrics
            self.metrics.record_metric(
                name="json_chunks_written",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas"},
                description="Number of chunks written to JSON files",
            )

            self.chunk_count += 1
            self.statistics.append(chunk_part)

        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="json_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "error": str(e)},
                description="Number of errors while writing to JSON files",
            )
            logger.error(f"Error writing dataframe to json: {str(e)}")

    async def write_daft_dataframe(
        self,
        dataframe: "daft.DataFrame",
        preserve_fields: Optional[List[str]] = [
            "identity_cycle",
            "number_columns_in_part_key",
            "columns_participating_in_part_key",
            "engine",
            "is_insertable_into",
            "is_typed",
        ],
        null_to_empty_dict_fields: Optional[List[str]] = [
            "attributes",
            "customAttributes",
        ],
    ):  # noqa: F821
        """Write a daft DataFrame to JSON files.

        This method converts the daft DataFrame to pandas and writes it to JSON files.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.

        Note:
            Daft does not have built-in JSON writing support, so we are using orjson.
        """
        try:
            buffer = []
            for row in dataframe.iter_rows():
                self.total_record_count += 1
                # Convert datetime fields to epoch timestamps before serialization
                row = convert_datetime_to_epoch(row)
                # Remove null attributes from the row recursively, preserving specified fields
                cleaned_row = self.process_null_fields(
                    row, preserve_fields, null_to_empty_dict_fields
                )
                # Serialize the row and add it to the buffer
                serialized_row = orjson.dumps(
                    cleaned_row, option=orjson.OPT_APPEND_NEWLINE
                )
                buffer.append(serialized_row)
                self.current_buffer_size_bytes += len(serialized_row)
                if (self.chunk_size and len(buffer) >= self.chunk_size) or (
                    self.current_buffer_size_bytes > self.max_file_size_bytes
                ):
                    await self.flush_daft_buffer(buffer)

            # Write any remaining rows in the buffer
            if buffer:
                await self.flush_daft_buffer(buffer)

            # Record metrics for successful write
            self.metrics.record_metric(
                name="json_write_records",
                value=dataframe.count_rows(),
                metric_type=MetricType.COUNTER,
                labels={"type": "daft"},
                description="Number of records written to JSON files from daft DataFrame",
            )
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="json_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "error": str(e)},
                description="Number of errors while writing to JSON files",
            )
            logger.error(f"Error writing daft dataframe to json: {str(e)}")

    async def flush_daft_buffer(self, buffer: List[str]):
        """Flush the current buffer to a JSON file.

        This method combines all DataFrames in the buffer, writes them to a JSON file,
        and uploads the file to the object store.
        """
        self.chunk_count += 1
        output_file_name = (
            f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count)}"
        )
        with open(output_file_name, "wb") as f:
            f.writelines(buffer)
        buffer.clear()  # Clear the buffer

        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0

        # Record chunk metrics
        self.metrics.record_metric(
            name="json_chunks_written",
            value=1,
            metric_type=MetricType.COUNTER,
            labels={"type": "daft"},
            description="Number of chunks written to JSON files",
        )

    async def write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a JSON file.

        This method writes a chunk to a JSON file and uploads the file to the object store.
        """
        mode = "w" if not os.path.exists(file_name) else "a"
        chunk.to_json(file_name, orient="records", lines=True, mode=mode)
