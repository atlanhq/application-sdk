import inspect
import os
import shutil
from enum import Enum
from typing import TYPE_CHECKING, AsyncGenerator, Generator, List, Optional, Union, cast

from temporalio import activity

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.constants import (
    DAPR_MAX_GRPC_MESSAGE_LENGTH,
    ENABLE_ATLAN_UPLOAD,
    UPSTREAM_OBJECT_STORE_NAME,
)
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
        output_suffix (str): Suffix for output files.
        typename (Optional[str]): Type name of the entity e.g database, schema, table.
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks created.
        chunk_start (Optional[int]): Starting index for chunk numbering.
        start_marker (Optional[str]): Start marker for query extraction.
        end_marker (Optional[str]): End marker for query extraction.
        retain_local_copy (bool): Whether to retain the local copy of the files.
        use_consolidation (bool): Whether to use consolidation.
    """

    _EXTENSION = ".parquet"

    def __init__(
        self,
        output_path: str = "",
        output_suffix: str = "",
        typename: Optional[str] = None,
        chunk_size: Optional[int] = 100000,
        buffer_size: int = 5000,
        total_record_count: int = 0,
        chunk_count: int = 0,
        chunk_part: int = 0,
        chunk_start: Optional[int] = None,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
        retain_local_copy: bool = False,
        use_consolidation: bool = False,
    ):
        """Initialize the Parquet output handler.

        Args:
            output_path (str): Base path where Parquet files will be written.
            output_suffix (str): Suffix for output files.
            typename (Optional[str], optional): Type name of the entity e.g database, schema, table.
            chunk_size (int, optional): Maximum records per chunk. Defaults to 100000.
            total_record_count (int, optional): Initial total record count. Defaults to 0.
            chunk_count (int, optional): Initial chunk count. Defaults to 0.
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            start_marker (Optional[str], optional): Start marker for query extraction.
                Defaults to None.
            end_marker (Optional[str], optional): End marker for query extraction.
                Defaults to None.
            retain_local_copy (bool, optional): Whether to retain the local copy of the files.
                Defaults to False.
            use_consolidation (bool, optional): Whether to use consolidation.
                Defaults to False.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.typename = typename
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.buffer: List[Union["pd.DataFrame", "daft.DataFrame"]] = []  # noqa: F821
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0  # Track estimated buffer size in bytes
        self.max_file_size_bytes = int(
            DAPR_MAX_GRPC_MESSAGE_LENGTH * 0.75
        )  # 75% of DAPR limit as safety buffer
        self.chunk_start = chunk_start
        self.chunk_part = chunk_part
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.partitions = []
        self.metrics = get_metrics()
        self.retain_local_copy = retain_local_copy

        # Consolidation-specific attributes
        # Use consolidation to efficiently write parquet files in buffered manner
        # since there's no cleaner way to write parquet files incrementally
        self.use_consolidation = use_consolidation
        self.consolidation_threshold = (
            chunk_size or 100000
        )  # Use chunk_size as threshold
        self.current_folder_records = 0  # Track records in current temp folder
        self.temp_folder_index = 0  # Current temp folder index
        self.temp_folders_created: List[int] = []  # Track temp folders for cleanup
        self.current_temp_folder_path: Optional[str] = None  # Current temp folder path

        if self.chunk_start:
            self.chunk_count = self.chunk_start + self.chunk_count

        # Create output directory
        self.output_path = os.path.join(self.output_path, self.output_suffix)
        if self.typename:
            self.output_path = os.path.join(self.output_path, self.typename)
        os.makedirs(self.output_path, exist_ok=True)

    async def write_batched_dataframe(
        self,
        batched_dataframe: Union[
            AsyncGenerator["pd.DataFrame", None], Generator["pd.DataFrame", None, None]
        ],
    ):
        """Write a batched pandas DataFrame to Parquet files with consolidation support.

        This method implements a consolidation strategy to efficiently write parquet files
        in a buffered manner, since there's no cleaner way to write parquet files incrementally.

        The process:
        1. Accumulate DataFrames into temp folders (buffer_size chunks each)
        2. When consolidation_threshold is reached, use Daft to merge into optimized files
        3. Clean up temporary files after consolidation

        Args:
            batched_dataframe: AsyncGenerator or Generator of pandas DataFrames to write.
        """
        if not self.use_consolidation:
            # Fallback to base class implementation
            await super().write_batched_dataframe(batched_dataframe)
            return

        try:
            # Phase 1: Accumulate DataFrames into temp folders
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self._accumulate_dataframe(dataframe)
            else:
                sync_generator = cast(
                    Generator["pd.DataFrame", None, None], batched_dataframe
                )
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self._accumulate_dataframe(dataframe)

            # Phase 2: Consolidate any remaining temp folder
            if self.current_folder_records > 0:
                await self._consolidate_current_folder()

            # Phase 3: Cleanup temp folders
            await self._cleanup_temp_folders()

        except Exception as e:
            logger.error(
                f"Error in batched dataframe writing with consolidation: {str(e)}"
            )
            await self._cleanup_temp_folders()  # Cleanup on error
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

            file_paths = []
            # Use Daft's execution context for temporary configuration
            with daft.execution_config_ctx(
                parquet_target_filesize=self.max_file_size_bytes,
                default_morsel_size=morsel_size,
            ):
                # Daft automatically handles file splitting and naming
                result = dataframe.write_parquet(
                    root_dir=self.output_path,
                    write_mode=write_mode.value,
                    partition_cols=partition_cols,
                )
                file_paths = result.to_pydict().get("path", [])

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
            for path in file_paths:
                if ENABLE_ATLAN_UPLOAD:
                    await ObjectStore.upload_file(
                        source=path,
                        store_name=UPSTREAM_OBJECT_STORE_NAME,
                        destination=get_object_store_prefix(path),
                        retain_local_copy=True,
                    )
                await ObjectStore.upload_file(
                    source=path,
                    destination=get_object_store_prefix(path),
                    retain_local_copy=self.retain_local_copy,
                )

        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="parquet_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={
                    "type": "daft",
                    "mode": write_mode.value
                    if isinstance(write_mode, WriteMode)
                    else write_mode,
                    "error": str(e),
                },
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

    # Consolidation helper methods

    def _get_temp_folder_path(self, folder_index: int) -> str:
        """Generate temp folder path consistent with existing structure."""
        temp_base_path = os.path.join(self.output_path, "temp_accumulation")
        return os.path.join(temp_base_path, f"folder-{folder_index}")

    def _get_consolidated_file_path(self, folder_index: int, chunk_part: int) -> str:
        """Generate final consolidated file path using existing path_gen logic."""
        return os.path.join(
            self.output_path,
            self.path_gen(chunk_count=folder_index, chunk_part=chunk_part),
        )

    async def _accumulate_dataframe(self, dataframe: "pd.DataFrame"):
        """Accumulate DataFrame into temp folders, writing in buffer_size chunks."""

        # Process dataframe in buffer_size chunks
        for i in range(0, len(dataframe), self.buffer_size):
            chunk = dataframe[i : i + self.buffer_size]

            # Check if we need to consolidate current folder before adding this chunk
            if (
                self.current_folder_records + len(chunk)
            ) > self.consolidation_threshold:
                if self.current_folder_records > 0:
                    await self._consolidate_current_folder()
                    self._start_new_temp_folder()

            # Ensure we have a temp folder ready
            if self.current_temp_folder_path is None:
                self._start_new_temp_folder()

            # Write chunk to current temp folder
            await self._write_chunk_to_temp_folder(cast("pd.DataFrame", chunk))
            self.current_folder_records += len(chunk)

    def _start_new_temp_folder(self):
        """Start a new temp folder for accumulation and create the directory."""
        if self.current_temp_folder_path is not None:
            self.temp_folders_created.append(self.temp_folder_index)
            self.temp_folder_index += 1

        self.current_folder_records = 0
        self.current_temp_folder_path = self._get_temp_folder_path(
            self.temp_folder_index
        )

        # Create the directory
        os.makedirs(self.current_temp_folder_path, exist_ok=True)

    async def _write_chunk_to_temp_folder(self, chunk: "pd.DataFrame"):
        """Write a chunk to the current temp folder."""
        if self.current_temp_folder_path is None:
            raise ValueError("No temp folder path available")

        # Generate file name for this chunk within the temp folder
        existing_files = len(
            [
                f
                for f in os.listdir(self.current_temp_folder_path)
                if f.endswith(".parquet")
            ]
        )
        chunk_file_name = f"chunk-{existing_files}.parquet"
        chunk_file_path = os.path.join(self.current_temp_folder_path, chunk_file_name)

        # Write chunk using existing write_chunk method
        await self.write_chunk(chunk, chunk_file_path)

    async def _consolidate_current_folder(self):
        """Consolidate current temp folder using Daft."""
        if self.current_folder_records == 0 or self.current_temp_folder_path is None:
            return

        try:
            import daft

            # Read all parquet files in temp folder
            pattern = os.path.join(self.current_temp_folder_path, "*.parquet")
            daft_df = daft.read_parquet(pattern)
            partitions = 0

            # Write consolidated file using Daft with size management
            with daft.execution_config_ctx(
                parquet_target_filesize=self.max_file_size_bytes
            ):
                # Write to a temp location first
                temp_consolidated_dir = f"{self.current_temp_folder_path}_temp"
                result = daft_df.write_parquet(root_dir=temp_consolidated_dir)

                # Get the generated file path and rename to final location
                result_dict = result.to_pydict()
                partitions = len(result_dict["path"])
                for i, file_path in enumerate(result_dict["path"]):
                    if file_path.endswith(".parquet"):
                        consolidated_file_path = self._get_consolidated_file_path(
                            folder_index=self.chunk_count,
                            chunk_part=i,
                        )
                        os.rename(file_path, consolidated_file_path)

                        # Upload consolidated file to object store
                        await ObjectStore.upload_file(
                            source=consolidated_file_path,
                            destination=get_object_store_prefix(consolidated_file_path),
                        )

                # Clean up temp consolidated dir
                shutil.rmtree(temp_consolidated_dir, ignore_errors=True)

            # Update statistics
            self.chunk_count += 1
            self.total_record_count += self.current_folder_records
            self.partitions.append(partitions)

            # Record metrics
            self.metrics.record_metric(
                name="consolidated_files",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft_consolidation"},
                description="Number of consolidated parquet files created",
            )

            logger.info(
                f"Consolidated folder {self.temp_folder_index} with {self.current_folder_records} records"
            )

        except Exception as e:
            logger.error(
                f"Error consolidating folder {self.temp_folder_index}: {str(e)}"
            )
            raise

    async def _cleanup_temp_folders(self):
        """Clean up all temp folders after consolidation."""
        try:
            # Add current folder to cleanup list if it exists
            if self.current_temp_folder_path is not None:
                self.temp_folders_created.append(self.temp_folder_index)

            # Clean up all temp folders
            for folder_index in self.temp_folders_created:
                temp_folder = self._get_temp_folder_path(folder_index)
                if os.path.exists(temp_folder):
                    shutil.rmtree(temp_folder, ignore_errors=True)

            # Clean up base temp directory if it exists and is empty
            temp_base_path = os.path.join(self.output_path, "temp_accumulation")
            if os.path.exists(temp_base_path) and not os.listdir(temp_base_path):
                os.rmdir(temp_base_path)

            # Reset state
            self.temp_folders_created.clear()
            self.current_temp_folder_path = None
            self.temp_folder_index = 0
            self.current_folder_records = 0

        except Exception as e:
            logger.warning(f"Error cleaning up temp folders: {str(e)}")

    async def write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a Parquet file.

        This method writes a chunk to a Parquet file and uploads the file to the object store.
        """
        chunk.to_parquet(file_name, index=False, compression="snappy")
