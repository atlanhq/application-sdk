import inspect
import os
import shutil
from typing import (
    TYPE_CHECKING,
    AsyncGenerator,
    AsyncIterator,
    Generator,
    List,
    Optional,
    Union,
    cast,
)

from temporalio import activity

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.constants import (
    DAPR_MAX_GRPC_MESSAGE_LENGTH,
    ENABLE_ATLAN_UPLOAD,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.io import DataframeType, Reader, WriteMode, Writer
from application_sdk.io.utils import (
    PARQUET_FILE_EXTENSION,
    download_files,
    is_empty_dataframe,
    path_gen,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class ParquetFileReader(Reader):
    """Parquet File Reader class to read data from Parquet files using daft and pandas.

    Supports reading both single files and directories containing multiple parquet files.
    Follows Python's file I/O pattern with read/close semantics and supports context managers.

    Attributes:
        path (str): Path to parquet file or directory containing parquet files.
        chunk_size (int): Number of rows per batch.
        buffer_size (int): Number of rows per batch for daft.
        file_names (Optional[List[str]]): List of specific file names to read.
        dataframe_type (DataframeType): Type of dataframe to return (pandas or daft).
        cleanup_on_close (bool): Whether to clean up downloaded temp files on close.

    Example:
        Using context manager (recommended)::

            async with ParquetFileReader(path="/data/input") as reader:
                df = await reader.read()
            # close() called automatically, temp files cleaned up

        Reading in batches::

            async with ParquetFileReader(path="/data/input", chunk_size=50000) as reader:
                async for batch in reader.read_batches():
                    process(batch)

        Using close() explicitly::

            reader = ParquetFileReader(path="/data/input")
            df = await reader.read()
            await reader.close()  # Clean up downloaded temp files
    """

    def __init__(
        self,
        path: str,
        chunk_size: Optional[int] = 100000,
        buffer_size: Optional[int] = 5000,
        file_names: Optional[List[str]] = None,
        dataframe_type: DataframeType = DataframeType.pandas,
        cleanup_on_close: bool = True,
    ):
        """Initialize the Parquet input class.

        Args:
            path (str): Path to parquet file or directory containing parquet files.
                It accepts both types of paths:
                local path or object store path
                Wildcards are not supported.
            chunk_size (int): Number of rows per batch. Defaults to 100000.
            buffer_size (int): Number of rows per batch. Defaults to 5000.
            file_names (Optional[List[str]]): List of file names to read. Defaults to None.
            dataframe_type (DataframeType): Type of dataframe to read. Defaults to DataframeType.pandas.
            cleanup_on_close (bool): Whether to clean up downloaded temp files on close. Defaults to True.

        Raises:
            ValueError: When path is not provided or when single file path is combined with file_names
        """

        # Validate that single file path and file_names are not both specified
        if path.endswith(PARQUET_FILE_EXTENSION) and file_names:
            raise ValueError(
                f"Cannot specify both a single file path ('{path}') and file_names filter. "
                f"Either provide a directory path with file_names, or specify the exact file path without file_names."
            )

        self.path = path
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.file_names = file_names
        self.dataframe_type = dataframe_type
        self.cleanup_on_close = cleanup_on_close
        self._is_closed = False
        self._downloaded_files: List[str] = []

    async def read(self) -> Union["pd.DataFrame", "daft.DataFrame"]:
        """Read the data from the parquet files and return as a single DataFrame.

        Returns:
            Union[pd.DataFrame, daft.DataFrame]: Combined dataframe from parquet files.

        Raises:
            ValueError: If the reader has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            raise ValueError("Cannot read from a closed reader")

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
        """Read the data from the parquet files and return as batched DataFrames.

        Returns:
            Union[AsyncIterator[pd.DataFrame], AsyncIterator[daft.DataFrame]]:
                Async iterator of DataFrames.

        Raises:
            ValueError: If the reader has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            raise ValueError("Cannot read from a closed reader")

        if self.dataframe_type == DataframeType.pandas:
            return self._get_batched_dataframe()
        elif self.dataframe_type == DataframeType.daft:
            return self._get_batched_daft_dataframe()
        else:
            raise ValueError(f"Unsupported dataframe_type: {self.dataframe_type}")

    async def _get_dataframe(self) -> "pd.DataFrame":
        """Read data from parquet file(s) and return as pandas DataFrame.

        Returns:
            pd.DataFrame: Combined dataframe from specified parquet files

        Raises:
            ValueError: When no valid path can be determined or no matching files found
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file3.parquet"]:
        +-------+-------+-------+
        | col1  | col2  | col3  |
        +-------+-------+-------+
        | val1  | val2  | val3  |  # from file1.parquet
        | val7  | val8  | val9  |  # from file3.parquet
        +-------+-------+-------+

        Transformations:
        - Only specified files are read and combined
        - Column schemas must be compatible across files
        - Only reads files in the specified directory
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            parquet_files = await download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info(f"Reading {len(parquet_files)} parquet files")

            return pd.concat(
                (pd.read_parquet(parquet_file) for parquet_file in parquet_files),
                ignore_index=True,
            )
        except Exception as e:
            logger.error(f"Error reading data from parquet file(s): {str(e)}")
            raise

    async def _get_batched_dataframe(
        self,
    ) -> AsyncIterator["pd.DataFrame"]:
        """Read data from parquet file(s) in batches as pandas DataFrames.

        Returns:
            AsyncIterator[pd.DataFrame]: Async iterator of pandas dataframes

        Raises:
            ValueError: When no parquet files found locally or in object store
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file2.parquet"] and chunk_size=2:
        Batch 1:
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val1  | val2  |  # from file1.parquet
        | val3  | val4  |  # from file1.parquet
        +-------+-------+

        Batch 2:
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val5  | val6  |  # from file2.parquet
        | val7  | val8  |  # from file2.parquet
        +-------+-------+

        Transformations:
        - Only specified files are combined then split into chunks
        - Each batch is a separate DataFrame
        - Only reads files in the specified directory
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            parquet_files = await download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info(f"Reading {len(parquet_files)} parquet files in batches")

            # Process each file individually to maintain memory efficiency
            for parquet_file in parquet_files:
                df = pd.read_parquet(parquet_file)
                for i in range(0, len(df), self.chunk_size):
                    yield df.iloc[i : i + self.chunk_size]  # type: ignore
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) in batches: {str(e)}"
            )
            raise

    async def _get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """Read data from parquet file(s) and return as daft DataFrame.

        Returns:
            daft.DataFrame: Combined daft dataframe from specified parquet files

        Raises:
            ValueError: When no parquet files found locally or in object store
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file3.parquet"]:
        +-------+-------+-------+
        | col1  | col2  | col3  |
        +-------+-------+-------+
        | val1  | val2  | val3  |  # from file1.parquet
        | val7  | val8  | val9  |  # from file3.parquet
        +-------+-------+-------+

        Transformations:
        - Only specified parquet files combined into single daft DataFrame
        - Lazy evaluation for better performance
        - Column schemas must be compatible across files
        """
        try:
            import daft  # type: ignore

            # Ensure files are available (local or downloaded)
            parquet_files = await download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info(f"Reading {len(parquet_files)} parquet files with daft")

            # Use the discovered/downloaded files directly
            return daft.read_parquet(parquet_files)
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) using daft: {str(e)}"
            )
            raise

    async def _get_batched_daft_dataframe(self) -> AsyncIterator["daft.DataFrame"]:  # type: ignore
        """Get batched daft dataframe from parquet file(s).

        Returns:
            AsyncIterator[daft.DataFrame]: An async iterator of daft DataFrames, each containing
            a batch of data from individual parquet files

        Raises:
            ValueError: When no parquet files found locally or in object store
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file3.parquet"]:
        Batch 1 (file1.parquet):
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val1  | val2  |
        | val3  | val4  |
        +-------+-------+

        Batch 2 (file3.parquet):
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val7  | val8  |
        | val9  | val10 |
        +-------+-------+

        Transformations:
        - Each specified file becomes a separate daft DataFrame batch
        - Lazy evaluation for better performance
        - Files processed individually for memory efficiency
        """
        try:
            import daft  # type: ignore

            # Ensure files are available (local or downloaded)
            parquet_files = await download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info(f"Reading {len(parquet_files)} parquet files as daft batches")

            # Create a lazy dataframe without loading data into memory
            lazy_df = daft.read_parquet(parquet_files)

            # Get total count efficiently
            total_rows = lazy_df.count_rows()

            # Yield chunks without loading everything into memory
            for offset in range(0, total_rows, self.buffer_size):
                chunk = lazy_df.offset(offset).limit(self.buffer_size)
                yield chunk

            del lazy_df

        except Exception as error:
            logger.error(
                f"Error reading data from parquet file(s) in batches using daft: {error}"
            )
            raise


class ParquetFileWriter(Writer):
    """Output handler for writing data to Parquet files.

    This class handles writing DataFrames to Parquet files with support for chunking
    and automatic uploading to object store.

    Attributes:
        path (str): Base path where Parquet files will be written.
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

    def __init__(
        self,
        path: str,
        typename: Optional[str] = None,
        chunk_size: Optional[int] = 100000,
        buffer_size: Optional[int] = 5000,
        total_record_count: Optional[int] = 0,
        chunk_count: Optional[int] = 0,
        chunk_part: Optional[int] = 0,
        chunk_start: Optional[int] = None,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
        retain_local_copy: Optional[bool] = False,
        use_consolidation: Optional[bool] = False,
        dataframe_type: DataframeType = DataframeType.pandas,
    ):
        """Initialize the Parquet output handler.

        Args:
            path (str): Base path where Parquet files will be written.
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
            dataframe_type (DataframeType, optional): Type of dataframe to write. Defaults to DataframeType.pandas.
        """
        self.extension = PARQUET_FILE_EXTENSION
        self.path = path
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
        self.dataframe_type = dataframe_type
        self._is_closed = False
        self._statistics = None

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

        if not self.path:
            raise ValueError("path is required")
        # Create output directory
        if self.typename:
            self.path = os.path.join(self.path, self.typename)
        os.makedirs(self.path, exist_ok=True)

    async def _write_batched_dataframe(
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
            await super()._write_batched_dataframe(batched_dataframe)
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

    async def _write_daft_dataframe(
        self,
        dataframe: "daft.DataFrame",  # noqa: F821
        partition_cols: Optional[List] = None,
        write_mode: Union[WriteMode, str] = WriteMode.APPEND.value,
        morsel_size: int = 100_000,
        **kwargs,
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
                    root_dir=self.path,
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
                        prefix=get_object_store_prefix(self.path)
                    )
                except FileNotFoundError as e:
                    logger.info(
                        f"No files found under prefix {get_object_store_prefix(self.path)}: {str(e)}"
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
        return self.path

    # Consolidation helper methods

    def _get_temp_folder_path(self, folder_index: int) -> str:
        """Generate temp folder path consistent with existing structure."""
        temp_base_path = os.path.join(self.path, "temp_accumulation")
        return os.path.join(temp_base_path, f"folder-{folder_index}")

    def _get_consolidated_file_path(self, folder_index: int, chunk_part: int) -> str:
        """Generate final consolidated file path using existing path_gen logic."""
        return os.path.join(
            self.path,
            path_gen(
                chunk_count=folder_index,
                chunk_part=chunk_part,
                extension=self.extension,
            ),
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
                if f.endswith(self.extension)
            ]
        )
        chunk_file_name = f"chunk-{existing_files}{self.extension}"
        chunk_file_path = os.path.join(self.current_temp_folder_path, chunk_file_name)

        # Write chunk using existing write_chunk method
        await self._write_chunk(chunk, chunk_file_path)

    async def _consolidate_current_folder(self):
        """Consolidate current temp folder using Daft."""
        if self.current_folder_records == 0 or self.current_temp_folder_path is None:
            return

        try:
            import daft

            # Read all parquet files in temp folder
            pattern = os.path.join(self.current_temp_folder_path, f"*{self.extension}")
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
                    if file_path.endswith(self.extension):
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
            temp_base_path = os.path.join(self.path, "temp_accumulation")
            if os.path.exists(temp_base_path) and not os.listdir(temp_base_path):
                os.rmdir(temp_base_path)

            # Reset state
            self.temp_folders_created.clear()
            self.current_temp_folder_path = None
            self.temp_folder_index = 0
            self.current_folder_records = 0

        except Exception as e:
            logger.warning(f"Error cleaning up temp folders: {str(e)}")

    async def _write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a Parquet file.

        This method writes a chunk to a Parquet file and uploads the file to the object store.
        """
        chunk.to_parquet(file_name, index=False, compression="snappy")
