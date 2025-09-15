import os
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Union

from temporalio import activity

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.outputs import Output
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class WriteMode(Enum):
    """Enumeration of write modes for Parquet output operations."""

    APPEND = "append"
    OVERWRITE = "overwrite"
    OVERWRITE_PARTITIONS = "overwrite-partitions"


class ParquetOutput(Output):
    """Output handler for writing data to Parquet files.

    This class handles writing DataFrames to Parquet files with support for chunking
    and automatic uploading to object store.

    Attributes:
        output_path (str): Base path where Parquet files will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        output_suffix (str): Suffix for output files.
        typename (Optional[str]): Type name of the entity e.g database, schema, table.
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks created.
        chunk_start (Optional[int]): Starting index for chunk numbering.
        path_gen (Callable): Function to generate file paths.
        start_marker (Optional[str]): Start marker for query extraction.
        end_marker (Optional[str]): End marker for query extraction.
    """

    def __init__(
        self,
        output_path: str = "",
        output_suffix: str = "",
        output_prefix: str = "",
        typename: Optional[str] = None,
        chunk_size: Optional[int] = 100000,
        buffer_size: Optional[int] = 100000,
        total_record_count: int = 0,
        chunk_count: int = 0,
        chunk_start: Optional[int] = None,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
        retain_local_copy: bool = False,
    ):
        """Initialize the Parquet output handler.

        Args:
            output_path (str): Base path where Parquet files will be written.
            output_suffix (str): Suffix for output files.
            output_prefix (str): Prefix for files when uploading to object store.
            typename (Optional[str], optional): Type name of the entity e.g database, schema, table.
            chunk_size (int, optional): Maximum records per chunk. Defaults to 100000.
            total_record_count (int, optional): Initial total record count. Defaults to 0.
            chunk_count (int, optional): Initial chunk count. Defaults to 0.
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            path_gen (Callable, optional): Function to generate file paths.
                Defaults to path_gen function.
            start_marker (Optional[str], optional): Start marker for query extraction.
                Defaults to None.
            end_marker (Optional[str], optional): End marker for query extraction.
                Defaults to None.
            retain_local_copy (bool, optional): Whether to retain the local copy of the files.
                Defaults to False.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.buffer: List[Union["pd.DataFrame", "daft.DataFrame"]] = []  # noqa: F821
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0  # Track estimated buffer size in bytes
        self.max_file_size_bytes = int(
            DAPR_MAX_GRPC_MESSAGE_LENGTH * 0.9
        )  # 90% of DAPR limit as safety buffer
        self.chunk_start = chunk_start
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.statistics = []
        self.metrics = get_metrics()
        self.retain_local_copy = retain_local_copy

        # Create output directory
        self.output_path = os.path.join(self.output_path, self.output_suffix)
        if self.typename:
            self.output_path = os.path.join(self.output_path, self.typename)
        os.makedirs(self.output_path, exist_ok=True)

    def path_gen(
        self,
        chunk_start: Optional[int] = None,
        chunk_count: int = 0,
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
            return f"{start_marker}_{end_marker}.parquet"

        # For regular chunking - include chunk count
        if chunk_start is None:
            return f"{str(chunk_count)}.parquet"
        else:
            return f"chunk-{str(chunk_start)}-part{str(chunk_count)}.parquet"

    async def write_dataframe(self, dataframe: "pd.DataFrame"):
        """Write a pandas DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
        """
        try:
            chunk_part = 0
            if len(dataframe) == 0:
                return

            # Split the DataFrame into chunks
            partition = (
                self.chunk_size
                if self.chunk_start is None
                else min(self.chunk_size, self.buffer_size)
            )
            chunks = [
                dataframe[i : i + partition]  # type: ignore
                for i in range(0, len(dataframe), partition)
            ]

            for chunk in chunks:
                # Estimate size of this chunk
                chunk_size_bytes = self.estimate_dataframe_file_size(chunk, "parquet")

                # Check if adding this chunk would exceed size limit
                if (
                    self.current_buffer_size_bytes + chunk_size_bytes
                    > self.max_file_size_bytes
                    and self.current_buffer_size > 0
                ):
                    # Flush current buffer before adding this chunk
                    chunk_part += 1
                    await self._flush_buffer(chunk_part)

                self.buffer.append(chunk)
                self.current_buffer_size += len(chunk)
                self.current_buffer_size_bytes += chunk_size_bytes

                if self.current_buffer_size >= partition:  # type: ignore
                    chunk_part += 1
                    await self._flush_buffer(chunk_part)

            if self.buffer and self.current_buffer_size > 0:
                chunk_part += 1
                await self._flush_buffer(chunk_part)

            # Record metrics for successful write
            self.metrics.record_metric(
                name="parquet_write_records",
                value=len(dataframe),
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": WriteMode.APPEND.value},
                description="Number of records written to Parquet files from pandas DataFrame",
            )

            # Record chunk metrics
            self.metrics.record_metric(
                name="parquet_chunks_written",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": WriteMode.APPEND.value},
                description="Number of chunks written to Parquet files",
            )

            self.chunk_count += 1
            self.statistics.append(chunk_part)
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="parquet_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={
                    "type": "pandas",
                    "mode": WriteMode.APPEND.value,
                    "error": str(e),
                },
                description="Number of errors while writing to Parquet files",
            )
            logger.error(f"Error writing pandas dataframe to parquet: {str(e)}")
            raise

    async def write_daft_dataframe(
        self,
        dataframe: "daft.DataFrame",  # noqa: F821
        partition_cols: Optional[List] = None,
        write_mode: Union[WriteMode, str] = WriteMode.APPEND,
        morsel_size: int = 100_000,
    ):
        """Write a daft DataFrame to Parquet files and upload to object store.

        Uses Daft's native file size management to automatically split large DataFrames
        into multiple parquet files based on the configured target file size. Supports
        Hive partitioning for efficient data organization.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
            partition_cols (Optional[List]): Column names or expressions to use for Hive partitioning.
                Can be strings (column names) or daft column expressions. If None (default), no partitioning is applied.
            write_mode (Union[WriteMode, str]): Write mode for parquet files.
                Use WriteMode.APPEND, WriteMode.OVERWRITE, WriteMode.OVERWRITE_PARTITIONS, or their string equivalents.
            morsel_size (int): Default number of rows in a morsel used for the new local executor, when running locally on just a single machine,
                Daft does not use partitions. Instead of using partitioning to control parallelism, the local execution engine performs a streaming-based
                execution on small "morsels" of data, which provides much more stable memory utilization while improving the user experience with not having
                to worry about partitioning.

        Note:
            - Daft automatically handles file chunking based on parquet_target_filesize
            - Multiple files will be created if DataFrame exceeds DAPR limit
            - If partition_cols is set, creates Hive-style directory structure
        """
        try:
            import daft

            # Convert string to enum if needed for backward compatibility
            if isinstance(write_mode, str):
                write_mode = WriteMode(write_mode)

            row_count = dataframe.count_rows()
            if row_count == 0:
                return

            # Use Daft's execution context for temporary configuration
            with daft.execution_config_ctx(
                parquet_target_filesize=self.max_file_size_bytes,
                default_morsel_size=morsel_size,
            ):
                # Daft automatically handles file splitting and naming
                dataframe.write_parquet(
                    root_dir=self.output_path,
                    write_mode=write_mode.value,
                    partition_cols=partition_cols if partition_cols else [],
                )

            # Update counters
            self.chunk_count += 1
            self.total_record_count += row_count

            # Record metrics for successful write
            self.metrics.record_metric(
                name="parquet_write_records",
                value=row_count,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "mode": write_mode.value},
                description="Number of records written to Parquet files from daft DataFrame",
            )

            # Record operation metrics (note: actual file count may be higher due to Daft's splitting)
            self.metrics.record_metric(
                name="parquet_write_operations",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "mode": write_mode.value},
                description="Number of write operations to Parquet files",
            )

            #  Upload the entire directory (contains multiple parquet files created by Daft)
            if write_mode == WriteMode.OVERWRITE:
                # Delete the directory from object store
                try:
                    await ObjectStore.delete_prefix(
                        prefix=get_object_store_prefix(self.output_path)
                    )
                except FileNotFoundError as e:
                    logger.info(
                        f"No files found under prefix {get_object_store_prefix(self.output_path)}: {str(e)}"
                    )

            await ObjectStore.upload_prefix(
                source=self.output_path,
                destination=get_object_store_prefix(self.output_path),
                retain_local_copy=self.retain_local_copy,
            )

        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="parquet_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "mode": write_mode, "error": str(e)},
                description="Number of errors while writing to Parquet files",
            )
            logger.error(f"Error writing daft dataframe to parquet: {str(e)}")
            raise

    def get_full_path(self) -> str:
        """Get the full path of the output file.

        Returns:
            str: The full path of the output file.
        """
        return self.output_path

    async def _flush_buffer(self, chunk_part: int):
        """Flush the current buffer to a Parquet file.

        This method combines all DataFrames in the buffer, writes them to a Parquet file,
        and uploads the file to the object store.

        Note:
            If the buffer is empty or has no records, the method returns without writing.
        """
        import pandas as pd

        if not self.buffer or not self.current_buffer_size:
            return

        if not all(isinstance(df, pd.DataFrame) for df in self.buffer):
            raise TypeError(
                "_flush_buffer encountered non-DataFrame elements in buffer. This should not happen."
            )

        try:
            # Now it's safe to cast for pd.concat
            pd_buffer: List[pd.DataFrame] = self.buffer  # type: ignore
            combined_dataframe = pd.concat(pd_buffer)

            # Write DataFrame to Parquet file
            if not combined_dataframe.empty:
                self.total_record_count += len(combined_dataframe)
                output_file_name = (
                    f"{self.output_path}/{self.path_gen(self.chunk_count, chunk_part)}"
                )
                combined_dataframe.to_parquet(
                    output_file_name, index=False, compression="snappy"
                )

                # Record chunk metrics
                self.metrics.record_metric(
                    name="parquet_chunks_written",
                    value=1,
                    metric_type=MetricType.COUNTER,
                    labels={"type": "pandas"},
                    description="Number of chunks written to Parquet files",
                )

                # Push the file to the object store
                await ObjectStore.upload_file(
                    source=output_file_name,
                    destination=get_object_store_prefix(output_file_name),
                )

            self.buffer.clear()
            self.current_buffer_size = 0
            self.current_buffer_size_bytes = 0

        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="parquet_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "error": str(e)},
                description="Number of errors while writing to Parquet files",
            )
            logger.error(f"Error flushing buffer to parquet: {str(e)}")
            raise e
