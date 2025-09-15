"""Parquet writer implementation for data output operations.

This module provides the ParquetWriter class for writing DataFrames to Parquet format.
It supports both pandas and daft DataFrames with consolidation support for optimal
file creation and handles parquet-specific processing like partitioning and compression.
"""

import os
import shutil
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Generator,
    List,
    Optional,
    Union,
    cast,
)

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.io import Writer
from application_sdk.io._utils import (
    is_daft_dataframe,
    is_empty_dataframe,
    normalize_to_async_iterator,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class WriteMode(Enum):
    """Enumeration of write modes for Parquet output operations."""

    APPEND = "append"
    OVERWRITE = "overwrite"
    OVERWRITE_PARTITIONS = "overwrite-partitions"


class ParquetWriter(Writer):
    """Writer for Parquet format output with automatic consolidation.

    This writer handles conversion of DataFrames to Parquet format with support
    for both pandas and daft DataFrames. It automatically uses consolidation for
    optimal parquet file creation and supports partitioning, compression, and
    various write modes.

    The writer accumulates data in temporary folders and consolidates using Daft
    for optimal file structure and performance. This approach ensures efficient
    file sizes and reduces the number of small files.

    Attributes:
        EXTENSION (str): File extension for Parquet files (".parquet")

    Example:
        >>> writer = ParquetWriter(
        ...     output_path="/tmp/data",
        ...     typename="user_data",
        ...     write_mode=WriteMode.APPEND
        ... )
        >>> await writer.write(dataframe, partition_cols=["date"])
        >>> stats = await writer.close()
    """

    EXTENSION = ".parquet"

    def __init__(
        self,
        output_path: str,
        chunk_size: int = 100000,
        buffer_size: int = 5000,
        write_mode: Union[WriteMode, str] = WriteMode.APPEND,
        **config: Any,
    ):
        """Initialize ParquetWriter with parquet-specific configuration.

        Args:
            output_path: Local directory where files will be written temporarily
            chunk_size: Consolidation threshold - number of records before consolidating (default: 100000)
            buffer_size: Number of records to buffer before writing to temp files (default: 5000)
            write_mode: Write mode for parquet files (default: WriteMode.APPEND)
            **config: Additional configuration options including:
                - max_file_size_bytes: Maximum file size before creating new part
                - retain_local_copy: Whether to keep local files after upload
        """
        super().__init__(
            output_path=output_path,
            chunk_size=chunk_size,
            buffer_size=buffer_size,
            **config,
        )

        # Convert string to enum if needed
        if isinstance(write_mode, str):
            write_mode = WriteMode(write_mode)

        self.write_mode = write_mode

        # Consolidation-specific attributes (from original ParquetOutput)
        self.consolidation_threshold = chunk_size  # Use chunk_size as threshold
        self.current_folder_records = 0  # Track records in current temp folder
        self.temp_folder_index = 0  # Current temp folder index
        self.temp_folders_created: List[int] = []  # Track temp folders for cleanup
        self.current_temp_folder_path: Optional[str] = None  # Current temp folder path

    async def write(
        self,
        data: Union[
            "pd.DataFrame",
            "daft.DataFrame",
            Generator["pd.DataFrame", None, None],
            AsyncGenerator["pd.DataFrame", None],
            Generator["daft.DataFrame", None, None],
            AsyncGenerator["daft.DataFrame", None],
        ],
        partition_cols: Optional[List] = None,
        write_mode: Optional[Union[WriteMode, str]] = None,
        morsel_size: int = 100_000,
        **format_options: Any,
    ) -> None:
        """Write data to Parquet format files with automatic consolidation.

        This method handles both pandas and daft DataFrames, routing them to
        appropriate processing based on type. Pandas DataFrames use consolidation
        for optimal file structure, while daft DataFrames are written directly.

        Args:
            data: DataFrame or generator of DataFrames to write
            partition_cols: Column names for Hive partitioning (daft DataFrames only)
            write_mode: Override default write mode for this operation
            morsel_size: Morsel size for daft execution (default: 100000)
            **format_options: Additional format-specific options (reserved for future use)

        Raises:
            ValueError: If data is invalid or unsupported type
            Exception: If writing operation fails
        """
        try:
            # Use provided write_mode or fall back to instance default
            effective_write_mode = write_mode or self.write_mode
            if isinstance(effective_write_mode, str):
                effective_write_mode = WriteMode(effective_write_mode)

            # Handle single daft DataFrame directly (like original write_daft_dataframe)
            if is_daft_dataframe(data):
                await self._write_daft_dataframe(
                    data, partition_cols, effective_write_mode, morsel_size
                )
                return

            # Handle pandas DataFrames and generators with consolidation
            await self._write_with_consolidation(data)

        except Exception as e:
            logger.error(f"Error writing data to Parquet: {e}")
            self._record_error_metrics("write_error", str(e))
            # Cleanup temp folders on error
            await self._cleanup_temp_folders()
            raise

    async def _write_with_consolidation(
        self,
        data: Union[
            "pd.DataFrame",
            Generator["pd.DataFrame", None, None],
            AsyncGenerator["pd.DataFrame", None],
        ],
    ) -> None:
        """Write pandas data with automatic consolidation for optimal file structure."""
        try:
            # Phase 1: Accumulate DataFrames into temp folders
            async for dataframe in normalize_to_async_iterator(data):
                if not is_empty_dataframe(dataframe):
                    await self._accumulate_dataframe(dataframe)

            # Phase 2: Consolidate any remaining temp folder
            if self.current_folder_records > 0:
                await self._consolidate_current_folder()

            # Phase 3: Cleanup temp folders
            await self._cleanup_temp_folders()

        except Exception as e:
            logger.error(f"Error in consolidation writing: {e}")
            await self._cleanup_temp_folders()  # Cleanup on error
            raise

    async def _write_daft_dataframe(
        self,
        dataframe: "daft.DataFrame",
        partition_cols: Optional[List] = None,
        write_mode: WriteMode = WriteMode.APPEND,
        morsel_size: int = 100_000,
    ) -> None:
        """Write a daft DataFrame to Parquet files (from original write_daft_dataframe)."""
        try:
            import daft

            row_count = dataframe.count_rows()
            if row_count == 0:
                return

            # Use Daft's execution context for temporary configuration
            with daft.execution_config_ctx(
                parquet_target_filesize=self.max_file_size_bytes,
                default_morsel_size=morsel_size,
            ):
                # Daft automatically handles file splitting and naming
                # Handle partition_cols properly to avoid Daft issues with empty lists
                dataframe.write_parquet(
                    root_dir=self.full_output_path,
                    write_mode=write_mode.value,
                    partition_cols=partition_cols,
                )

            # Update counters
            self.chunk_count += 1
            self.total_record_count += row_count

            # Record metrics for successful write
            self._record_success_metrics(
                operation=f"daft_{write_mode.value}",
                record_count=row_count,
                description="Number of records written to Parquet files from daft DataFrame",
            )

            # Handle overwrite mode - delete from object store first
            if write_mode == WriteMode.OVERWRITE:
                try:
                    await ObjectStore.delete_prefix(
                        prefix=get_object_store_prefix(self.full_output_path)
                    )
                except FileNotFoundError as e:
                    logger.info(
                        f"No files found under prefix {get_object_store_prefix(self.full_output_path)}: {e}"
                    )

            # Files will be uploaded by base class close() method
        except Exception as e:
            # Record metrics for failed write
            self._record_error_metrics(
                error_type=f"daft_{write_mode.value}_error",
                error_message=str(e),
                description="Number of errors while writing to Parquet files",
            )
            logger.error(f"Error writing daft dataframe to parquet: {e}")
            raise

    async def _write_buffer_to_file(
        self, buffer: List["pd.DataFrame"], file_path: str
    ) -> None:
        """Write buffer contents to Parquet file (required by base Writer class)."""
        if not buffer:
            return

        # Concatenate all DataFrames in buffer
        import pandas as pd

        combined_df = pd.concat(buffer, ignore_index=True)

        # Write to parquet file
        combined_df.to_parquet(file_path, index=False, compression="snappy")

        logger.info(f"Wrote {len(buffer)} parquet chunks to {file_path}")

    async def _finalize_current_file(self) -> None:
        """Finalize the current chunk file and update statistics (parquet-specific).

        With consolidation as default, files are already finalized during the consolidation
        process, so this method is a no-op to avoid double-counting.
        """
        # With consolidation, files are already processed and added to chunks_info
        # during the consolidation process, so no additional finalization is needed
        pass

    def _get_temp_folder_path(self, folder_index: int) -> str:
        """Generate temp folder path consistent with existing structure."""
        temp_base_path = os.path.join(self.full_output_path, "temp_accumulation")
        return os.path.join(temp_base_path, f"folder-{folder_index}")

    def _get_consolidated_file_path(self, folder_index: int) -> str:
        """Generate final consolidated file path using existing path_gen logic."""
        return os.path.join(
            self.full_output_path, f"chunk-{folder_index}-part0{self.EXTENSION}"
        )

    async def _accumulate_dataframe(self, dataframe: "pd.DataFrame") -> None:
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

    def _start_new_temp_folder(self) -> None:
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

    async def _write_chunk_to_temp_folder(self, chunk: "pd.DataFrame") -> None:
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

        # Write chunk directly to parquet
        chunk.to_parquet(chunk_file_path, index=False, compression="snappy")

    async def _consolidate_current_folder(self) -> None:
        """Consolidate current temp folder using Daft."""
        if self.current_folder_records == 0 or self.current_temp_folder_path is None:
            return

        try:
            import daft

            # Read all parquet files in temp folder
            pattern = os.path.join(self.current_temp_folder_path, "*.parquet")
            daft_df = daft.read_parquet(pattern)

            # Generate consolidated file path
            consolidated_file_path = self._get_consolidated_file_path(
                self.temp_folder_index
            )

            # Write consolidated file using Daft with size management
            with daft.execution_config_ctx(
                parquet_target_filesize=self.max_file_size_bytes
            ):
                # Write to a temp location first
                temp_consolidated_dir = f"{consolidated_file_path}_temp"
                result = daft_df.write_parquet(root_dir=temp_consolidated_dir)

                # Get the generated file path and rename to final location
                result_dict = result.to_pydict()
                generated_file = result_dict["path"][0]
                os.rename(generated_file, consolidated_file_path)

                # Clean up temp consolidated dir
                shutil.rmtree(temp_consolidated_dir, ignore_errors=True)

            # Get file info for statistics
            filename = os.path.basename(consolidated_file_path)
            file_size = os.path.getsize(consolidated_file_path)

            # Files will be uploaded by base class close() method

            # Add to chunks_info manually since consolidation bypasses normal flow
            self.chunks_info.append(
                {
                    "file_name": filename,
                    "record_count": self.current_folder_records,
                    "file_size_bytes": file_size,
                }
            )

            # Update counters
            self.chunk_count += 1
            self.total_record_count += self.current_folder_records

            # Record metrics for consolidation
            self._record_success_metrics(
                operation="consolidation",
                record_count=self.current_folder_records,
                description="Number of records consolidated into parquet files",
            )

            logger.info(
                f"Consolidated folder {self.temp_folder_index} with {self.current_folder_records} records"
            )
        except Exception as e:
            logger.error(f"Error consolidating folder {self.temp_folder_index}: {e}")
            raise

    async def _cleanup_temp_folders(self) -> None:
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
            temp_base_path = os.path.join(self.full_output_path, "temp_accumulation")
            if os.path.exists(temp_base_path) and not os.listdir(temp_base_path):
                os.rmdir(temp_base_path)

            # Reset state
            self.temp_folders_created.clear()
            self.current_temp_folder_path = None
            self.temp_folder_index = 0
            self.current_folder_records = 0

        except Exception as e:
            logger.warning(f"Error cleaning up temp folders: {e}")
