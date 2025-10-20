import os
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional, Union

import orjson
from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.common.types import DataframeType
from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.io._utils import (
    JSON_FILE_EXTENSION,
    convert_datetime_to_epoch,
    download_files,
    path_gen,
    process_null_fields,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics

if TYPE_CHECKING:
    import daft
    import pandas as pd

from application_sdk.io import Reader, Writer

logger = get_logger(__name__)
activity.logger = logger


class JsonFileReader(Reader):
    """
    JSON File Reader class to read data from JSON files using daft and pandas.
    Supports reading both single files and directories containing multiple JSON files.
    """

    def __init__(
        self,
        path: str,
        file_names: Optional[List[str]] = None,
        chunk_size: int = 100000,
        dataframe_type: DataframeType = DataframeType.pandas,
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): Path to JSON file or directory containing JSON files.
                It accepts both types of paths:
                local path or object store path
                Wildcards are not supported.
            file_names (Optional[List[str]]): List of specific file names to read. Defaults to None.
            chunk_size (int): Number of rows per batch. Defaults to 100000.

        Raises:
            ValueError: When path is not provided or when single file path is combined with file_names
        """
        self.extension = JSON_FILE_EXTENSION

        # Validate that single file path and file_names are not both specified
        if path.endswith(self.extension) and file_names:
            raise ValueError(
                f"Cannot specify both a single file path ('{path}') and file_names filter. "
                f"Either provide a directory path with file_names, or specify the exact file path without file_names."
            )

        self.path = path
        self.chunk_size = chunk_size
        self.file_names = file_names
        self.dataframe_type = dataframe_type

    async def read(self) -> Union["pd.DataFrame", "daft.DataFrame"]:
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        if self.dataframe_type == DataframeType.pandas:
            return await self._get_dataframe()
        elif self.dataframe_type == DataframeType.daft:
            return await self._get_daft_dataframe()
        else:
            raise ValueError(f"Unsupported dataframe_type: {self.dataframe_type}")

    def read_batches(
        self,
    ) -> Union[
        AsyncIterator["pd.DataFrame"],
        AsyncIterator["daft.DataFrame"],
    ]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        if self.dataframe_type == DataframeType.pandas:
            return self._get_batched_dataframe()
        elif self.dataframe_type == DataframeType.daft:
            return self._get_batched_daft_dataframe()
        else:
            raise ValueError(f"Unsupported dataframe_type: {self.dataframe_type}")

    async def _get_batched_dataframe(
        self,
    ) -> AsyncIterator["pd.DataFrame"]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            json_files = await download_files(
                self.path, self.extension, self.file_names
            )
            logger.info(f"Reading {len(json_files)} JSON files in batches")

            for json_file in json_files:
                json_reader_obj = pd.read_json(
                    json_file,
                    chunksize=self.chunk_size,
                    lines=True,
                )
                for chunk in json_reader_obj:
                    yield chunk
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")
            raise

    async def _get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            json_files = await download_files(
                self.path, self.extension, self.file_names
            )
            logger.info(f"Reading {len(json_files)} JSON files as pandas dataframe")

            return pd.concat(
                (pd.read_json(json_file, lines=True) for json_file in json_files),
                ignore_index=True,
            )

        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")
            raise

    async def _get_batched_daft_dataframe(
        self,
    ) -> AsyncIterator["daft.DataFrame"]:  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a batched daft dataframe
        """
        try:
            import daft

            # Ensure files are available (local or downloaded)
            json_files = await download_files(
                self.path, self.extension, self.file_names
            )
            logger.info(f"Reading {len(json_files)} JSON files as daft batches")

            # Yield each discovered file as separate batch with chunking
            for json_file in json_files:
                yield daft.read_json(json_file, _chunk_size=self.chunk_size)
        except Exception as e:
            logger.error(f"Error reading batched data from JSON using daft: {str(e)}")
            raise

    async def _get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        try:
            import daft

            # Ensure files are available (local or downloaded)
            json_files = await download_files(
                self.path, self.extension, self.file_names
            )
            logger.info(f"Reading {len(json_files)} JSON files with daft")

            # Use the discovered/downloaded files directly
            return daft.read_json(json_files)
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
            raise


class JsonFileWriter(Writer):
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
        typename: Optional[str] = None,
        chunk_start: Optional[int] = None,
        buffer_size: int = 5000,
        chunk_size: Optional[int] = 50000,  # to limit the memory usage on upload
        total_record_count: int = 0,
        chunk_count: int = 0,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
        retain_local_copy: bool = False,
        dataframe_type: DataframeType = DataframeType.pandas,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JSON output handler.

        Args:
            output_path (str): Path where JSON files will be written.
            output_suffix (str): Prefix for files when uploading to object store.
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
            retain_local_copy (bool, optional): Whether to retain the local copy of the files.
                Defaults to False.
            dataframe_type (DataframeType, optional): Type of dataframe to write. Defaults to DataframeType.pandas.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.typename = typename
        self.chunk_start = chunk_start
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.buffer_size = buffer_size
        self.chunk_size = chunk_size or 50000  # to limit the memory usage on upload
        self.buffer: List[Union["pd.DataFrame", "daft.DataFrame"]] = []  # noqa: F821
        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0  # Track estimated buffer size in bytes
        self.max_file_size_bytes = int(
            DAPR_MAX_GRPC_MESSAGE_LENGTH * 0.9
        )  # 90% of DAPR limit as safety buffer
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.partitions = []
        self.chunk_part = 0
        self.metrics = get_metrics()
        self.retain_local_copy = retain_local_copy
        self.extension = JSON_FILE_EXTENSION
        self.dataframe_type = dataframe_type

        if not self.output_path:
            raise ValueError("output_path is required")

        self.output_path = os.path.join(self.output_path, output_suffix)
        if typename:
            self.output_path = os.path.join(self.output_path, typename)
        os.makedirs(self.output_path, exist_ok=True)

        if self.chunk_start:
            self.chunk_count = self.chunk_start + self.chunk_count

    async def _write_daft_dataframe(
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
        **kwargs,
    ):  # noqa: F821
        """Write a daft DataFrame to JSON files.

        This method converts the daft DataFrame to pandas and writes it to JSON files.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.

        Note:
            Daft does not have built-in JSON writing support, so we are using orjson.
        """
        try:
            if self.chunk_start is None:
                self.chunk_part = 0

            buffer = []
            for row in dataframe.iter_rows():
                self.total_record_count += 1
                # Convert datetime fields to epoch timestamps before serialization
                row = convert_datetime_to_epoch(row)
                # Remove null attributes from the row recursively, preserving specified fields
                cleaned_row = process_null_fields(
                    row, preserve_fields, null_to_empty_dict_fields
                )
                # Serialize the row and add it to the buffer
                serialized_row = orjson.dumps(
                    cleaned_row, option=orjson.OPT_APPEND_NEWLINE
                )
                buffer.append(serialized_row)
                self.current_buffer_size += 1
                self.current_buffer_size_bytes += len(serialized_row)

                # If the buffer size is reached append to the file and clear the buffer
                if self.current_buffer_size >= self.buffer_size:
                    await self._flush_daft_buffer(buffer, self.chunk_part)

                if self.current_buffer_size_bytes > self.max_file_size_bytes or (
                    self.total_record_count > 0
                    and self.total_record_count % self.chunk_size == 0
                ):
                    output_file_name = f"{self.output_path}/{path_gen(self.chunk_count, self.chunk_part, self.start_marker, self.end_marker, extension=self.extension)}"
                    if os.path.exists(output_file_name):
                        await self._upload_file(output_file_name)
                        self.chunk_part += 1

            # Write any remaining rows in the buffer
            if self.current_buffer_size > 0:
                await self._flush_daft_buffer(buffer, self.chunk_part)

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

    async def _flush_daft_buffer(self, buffer: List[str], chunk_part: int):
        """Flush the current buffer to a JSON file.

        This method combines all DataFrames in the buffer, writes them to a JSON file,
        and uploads the file to the object store.
        """
        output_file_name = f"{self.output_path}/{path_gen(self.chunk_count, chunk_part, extension=self.extension)}"
        with open(output_file_name, "ab+") as f:
            f.writelines(buffer)
        buffer.clear()  # Clear the buffer

        self.current_buffer_size = 0

        # Record chunk metrics
        self.metrics.record_metric(
            name="json_chunks_written",
            value=1,
            metric_type=MetricType.COUNTER,
            labels={"type": "daft"},
            description="Number of chunks written to JSON files",
        )

    async def _write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a JSON file.

        This method writes a chunk to a JSON file and uploads the file to the object store.
        """
        mode = "w" if not os.path.exists(file_name) else "a"
        chunk.to_json(file_name, orient="records", lines=True, mode=mode)

    async def get_statistics(
        self, typename: Optional[str] = None
    ) -> ActivityStatistics:
        """Get the statistics of the JSON files.

        This method returns the statistics of the JSON files.
        """
        # Finally upload the final file
        if self.current_buffer_size_bytes > 0:
            output_file_name = f"{self.output_path}/{path_gen(self.chunk_count, self.chunk_part, extension=self.extension)}"
            if os.path.exists(output_file_name):
                await self._upload_file(output_file_name)
                self.chunk_part += 1

        # If chunk_start is set we don't want to increment the chunk_count
        # Since it should only increment the chunk_part in this case
        if self.chunk_start is None:
            self.chunk_count += 1
        self.partitions.append(self.chunk_part)

        return await super().get_statistics(typename)
