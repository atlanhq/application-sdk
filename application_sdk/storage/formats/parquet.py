import inspect
import os
import uuid
import warnings
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from typing import TYPE_CHECKING, Union, cast

from application_sdk.common.exc_utils import rewrap
from application_sdk.common.file_ops import SafeFileOps
from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.storage.batch import delete_prefix as _delete_prefix
from application_sdk.storage.formats import DataframeType, Reader, WriteMode, Writer
from application_sdk.storage.formats.utils import (
    PARQUET_FILE_EXTENSION,
    _download_files,
    is_empty_dataframe,
    path_gen,
)
from application_sdk.storage.ops import normalize_key
from application_sdk.storage.ops import upload_file as _upload_file

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd

    from application_sdk.contracts.types import FileReference


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
        chunk_size: int | None = 100000,
        buffer_size: int | None = 5000,
        file_names: list[str] | None = None,
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
        warnings.warn(
            "ParquetFileReader is deprecated and will be removed in v4.0. "
            "Receive a FileReference on your task's typed Input — the SDK "
            "auto-materializes it to a local path before the task runs, then "
            "read it directly with pandas.read_parquet / daft.read_parquet. "
            "See docs/agents/coding-standards.md.",
            DeprecationWarning,
            stacklevel=2,
        )

        # Validate that single file path and file_names are not both specified
        if path.endswith(PARQUET_FILE_EXTENSION) and file_names:
            raise ValueError(
                f"Cannot specify both a single file path ('{path}') and file_names filter. "
                f"Either provide a directory path with file_names, or specify the exact file path without file_names."
            )

        # Initialise the Reader base class so `_is_closed` and
        # `_downloaded_files` are per-instance state (not shared via the old
        # class-level mutable defaults). Required after BLDX-1167.
        super().__init__()
        self.path = path
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.file_names = file_names
        self.dataframe_type = dataframe_type
        self.cleanup_on_close = cleanup_on_close

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
    ) -> AsyncIterator["pd.DataFrame"] | AsyncIterator["daft.DataFrame"]:
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
            import pandas as pd  # noqa: PLC0415 — optional dep: pandas

            # Ensure files are available (local or downloaded)
            parquet_files = await _download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info("Reading %d parquet files", len(parquet_files))

            return pd.concat(
                (pd.read_parquet(parquet_file) for parquet_file in parquet_files),
                ignore_index=True,
            )
        except Exception as e:
            raise rewrap(e, "Error reading data from parquet file(s)") from e

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
            import pandas as pd  # noqa: PLC0415 — optional dep: pandas

            # Ensure files are available (local or downloaded)
            parquet_files = await _download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info("Reading %d parquet files in batches", len(parquet_files))

            # Process each file individually to maintain memory efficiency
            for parquet_file in parquet_files:
                df = pd.read_parquet(parquet_file)
                for i in range(0, len(df), self.chunk_size):
                    yield df.iloc[i : i + self.chunk_size]  # type: ignore
        except Exception as e:
            raise rewrap(e, "Error reading data from parquet file(s) in batches") from e

    async def _get_daft_dataframe(self) -> "daft.DataFrame":
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
            import daft  # noqa: PLC0415 — optional dep: daft

            # Ensure files are available (local or downloaded)
            parquet_files = await _download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info("Reading %d parquet files with daft", len(parquet_files))

            # Use the discovered/downloaded files directly
            return daft.read_parquet(parquet_files)
        except Exception as e:
            raise rewrap(e, "Error reading data from parquet file(s) using daft") from e

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
            import daft  # noqa: PLC0415 — optional dep: daft

            # Ensure files are available (local or downloaded)
            parquet_files = await _download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info("Reading %d parquet files as daft batches", len(parquet_files))

            # Unify parquet schemas before reading: when early files have
            # null-typed columns and later files have string-typed columns,
            # daft silently drops all data. We read each file's schema
            # metadata (cheap), unify via pyarrow, and pass the result to
            # daft so it reads all files with consistent types. See BLDX-837.
            daft_schema = None
            try:
                import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow
                import pyarrow.parquet as pq_meta  # noqa: PLC0415 — optional dep: pyarrow.parquet

                pa_schemas = [pq_meta.read_schema(f) for f in parquet_files]
                unified = pa.unify_schemas(pa_schemas, promote_options="permissive")
                daft_schema = {
                    field.name: daft.DataType.from_arrow_type(
                        pa.large_string()
                        if pa.types.is_null(field.type)
                        else field.type
                    )
                    for field in unified
                }
            except Exception:
                logger.debug(
                    "Could not unify parquet schemas, falling back to daft default schema inference",
                    exc_info=True,
                )

            lazy_df = daft.read_parquet(parquet_files, schema=daft_schema)
            total_rows = lazy_df.count_rows()

            for offset in range(0, total_rows, self.buffer_size):
                chunk = lazy_df.offset(offset).limit(self.buffer_size)
                yield chunk

            del lazy_df

        except Exception:
            logger.error(
                "Error reading data from parquet file(s) in batches using daft",
                exc_info=True,
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
        replace_prefix (bool): Whether to clear the existing object-store prefix before
            the first write.
    """

    def __init__(
        self,
        path: str,
        typename: str | None = None,
        chunk_size: int | None = 100000,
        buffer_size: int | None = 5000,
        total_record_count: int | None = 0,
        chunk_count: int | None = 0,
        chunk_part: int | None = 0,
        chunk_start: int | None = None,
        start_marker: str | None = None,
        end_marker: str | None = None,
        retain_local_copy: bool | None = False,
        use_consolidation: bool | None = False,
        dataframe_type: DataframeType = DataframeType.pandas,
        replace_prefix: bool = False,
        defer_uploads: bool = False,
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
            replace_prefix (bool, optional): Clear existing object-store keys under
                the writer prefix before the first write. Defaults to False.
            defer_uploads (bool, optional): When False (default), the writer
                uploads each chunk inline as on previous SDK versions — fully
                backwards compatible. When True, no inline uploads happen on
                any code path; the writer hands back an ephemeral
                ``FileReference`` on ``close()`` (in ``result.files``) and the
                Temporal activity interceptor uploads the directory with
                SHA-256 sidecars + parallel transfers when the task returns.
                Apps adopt the deferred path at their own pace; existing apps
                that ignore this flag see no behaviour change.
        """
        # DeprecationWarning: ParquetFileWriter is planned for removal in v4.0.
        # The recommended forward pattern is to write parquet locally with
        # pandas/daft directly and return a FileReference for the output
        # directory; the activity interceptor handles persistence.
        warnings.warn(
            "ParquetFileWriter is deprecated and will be removed in v4.0. "
            "Use pandas/daft to write parquet locally and return a "
            "FileReference for the output directory — the activity "
            "interceptor will persist it with SHA-256 sidecars and parallel "
            "transfers. See docs/agents/coding-standards.md for the canonical "
            "pattern.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.extension = PARQUET_FILE_EXTENSION
        self.path = path
        self.typename = typename
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.buffer: list[pd.DataFrame | daft.DataFrame] = []
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
        self.replace_prefix = replace_prefix
        self._prefix_replaced = False
        self.defer_uploads = defer_uploads

        # Consolidation-specific attributes
        # Use consolidation to efficiently write parquet files in buffered manner
        # since there's no cleaner way to write parquet files incrementally
        self.use_consolidation = use_consolidation
        self.consolidation_threshold = (
            chunk_size or 100000
        )  # Use chunk_size as threshold
        self.current_folder_records = 0  # Track records in current temp folder
        self.temp_folder_index = 0  # Current temp folder index
        self.temp_folders_created: list[int] = []  # Track temp folders for cleanup
        self.current_temp_folder_path: str | None = None  # Current temp folder path

        if self.chunk_start:
            self.chunk_count = self.chunk_start + self.chunk_count

        if not self.path:
            raise ValueError("path is required")
        # Create output directory. When typename is set, behaviour matches
        # main exactly (`<path>/<typename>/`). When typename is absent AND
        # deferred uploads are on, the writer creates its own scoped
        # sub-directory so the resulting FileReference covers only this
        # writer's chunks — never sibling content in the caller's `path`.
        # Default mode (defer_uploads=False) preserves main's behaviour
        # for callers that pass a bare `path` with no typename.
        if self.typename:
            self.path = os.path.join(self.path, self.typename)
        elif self.defer_uploads:
            self.path = os.path.join(self.path, f"_parquet_{uuid.uuid4().hex[:8]}")
        SafeFileOps.makedirs(self.path, exist_ok=True)

    async def _ensure_prefix_replaced(self) -> None:
        """Clear the object-store prefix once for replacing writes."""
        if not self.replace_prefix or self._prefix_replaced:
            return

        normalized_prefix = normalize_key(self.path)
        if not normalized_prefix:
            raise ValueError(
                "replace_prefix=True requires a non-empty object-store prefix"
            )

        try:
            deleted_count = await _delete_prefix(self.path)
        except RuntimeError as exc:
            if "No ObjectStore provided" not in str(exc):
                raise
            logger.debug("No object store configured, skipping prefix replacement")
        else:
            logger.info(
                "Cleared existing parquet object-store prefix",
                prefix=normalized_prefix,
                deleted_count=deleted_count,
            )
        self._prefix_replaced = True

    async def _write_dataframe(self, dataframe: "pd.DataFrame", **kwargs):
        """Write a pandas DataFrame after optional prefix replacement."""
        await self._ensure_prefix_replaced()
        await super()._write_dataframe(dataframe, **kwargs)

    async def _write_batched_dataframe(
        self,
        batched_dataframe: AsyncGenerator["pd.DataFrame", None]
        | Generator["pd.DataFrame", None, None],
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
        await self._ensure_prefix_replaced()

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

        except Exception:
            logger.error(
                "Error in batched dataframe writing with consolidation", exc_info=True
            )
            await self._cleanup_temp_folders()  # Cleanup on error
            raise

    async def _write_daft_dataframe(
        self,
        dataframe: "daft.DataFrame",
        partition_cols: list | None = None,
        write_mode: WriteMode | str = WriteMode.APPEND.value,
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
            await self._ensure_prefix_replaced()

            import daft  # noqa: PLC0415 — optional dep: daft

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

            # Upload the entire directory (contains multiple parquet files
            # created by Daft). When defer_uploads=True the caller persists
            # via close()'s returned FileReference instead — skip inline.
            if not self.defer_uploads:
                if write_mode == WriteMode.OVERWRITE:
                    # Delete the directory from object store
                    try:
                        await _delete_prefix(self.path)
                    except FileNotFoundError:
                        logger.info("No files found under prefix: %s", self.path)
                for path in file_paths:
                    await _upload_file(
                        path, path, retain_local_copy=self.retain_local_copy
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
                    "error_type": type(e).__name__,
                },
                description="Number of errors while writing to Parquet files",
            )
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
            ) > self.consolidation_threshold and self.current_folder_records > 0:
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
        SafeFileOps.makedirs(self.current_temp_folder_path, exist_ok=True)

    async def _write_chunk_to_temp_folder(self, chunk: "pd.DataFrame"):
        """Write a chunk to the current temp folder."""
        if self.current_temp_folder_path is None:
            raise ValueError("No temp folder path available")

        # Generate file name for this chunk within the temp folder
        existing_files = len(
            [
                f
                for f in SafeFileOps.listdir(self.current_temp_folder_path)
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
            import daft  # noqa: PLC0415 — optional dep: daft

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
                        SafeFileOps.rename(file_path, consolidated_file_path)

                        # Upload consolidated file to object store inline
                        # (skipped when defer_uploads=True — caller persists
                        # via close()'s returned FileReference).
                        if not self.defer_uploads:
                            await _upload_file(
                                consolidated_file_path, consolidated_file_path
                            )

                # Clean up temp consolidated dir
                SafeFileOps.rmtree(temp_consolidated_dir, ignore_errors=True)

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
                "Consolidated folder index=%d record_count=%d",
                self.temp_folder_index,
                self.current_folder_records,
            )

        except Exception:
            logger.error(
                "Error consolidating folder %s",
                self.temp_folder_index,
                exc_info=True,
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
                if SafeFileOps.exists(temp_folder):
                    SafeFileOps.rmtree(temp_folder, ignore_errors=True)

            # Clean up base temp directory if it exists and is empty
            temp_base_path = os.path.join(self.path, "temp_accumulation")
            if SafeFileOps.exists(temp_base_path) and not SafeFileOps.listdir(
                temp_base_path
            ):
                SafeFileOps.rmdir(temp_base_path)

            # Reset state
            self.temp_folders_created.clear()
            self.current_temp_folder_path = None
            self.temp_folder_index = 0
            self.current_folder_records = 0

        except Exception:
            logger.warning("Error cleaning up temp folders", exc_info=True)

    async def _flush_buffer(self, chunk: "pd.DataFrame", chunk_part: int):
        """Flush a buffer chunk to a Parquet file, upload it, and advance chunk_part.

        Overrides base Writer._flush_buffer because Parquet files cannot be
        appended to (unlike JSON where _write_chunk uses open("a")).
        pq.write_table() always overwrites the target file, so without this
        override _write_dataframe's buffer loop writes every sub-chunk to the
        same filename, silently losing all data except the last sub-chunk.

        Each parquet sub-chunk is complete after write (no appending), so when
        ``defer_uploads=False`` we upload immediately — the base class's
        post-loop upload only handles the last file and would miss the
        intermediate sub-chunks (HYP-773). When ``defer_uploads=True``, no
        inline upload happens; ``close()``'s returned ``FileReference``
        carries the entire output directory to the activity interceptor.
        """
        await super()._flush_buffer(chunk, chunk_part)
        if not self.defer_uploads:
            output_file_name = f"{self.path}/{path_gen(self.chunk_count, chunk_part, extension=self.extension)}"
            if os.path.exists(output_file_name):
                try:
                    await self._upload_file(output_file_name)
                except RuntimeError:
                    # No object store configured (local dev) — file stays on disk.
                    logger.debug("No object store configured, skipping upload")
        # Advance part so the next sub-chunk gets a unique filename.
        self.chunk_part += 1

    async def _upload_file(self, file_name: str) -> None:
        """Upload a file to the object store, or no-op when uploads are deferred.

        With ``defer_uploads=True`` this overrides the base implementation
        to a no-op so every base-class call site (overflow check, final
        flush, statistics sidecar) skips inline uploads. The caller persists
        via ``close()``'s returned ``FileReference`` instead.

        With ``defer_uploads=False`` we delegate to the base, preserving the
        pre-BLDX-1136 inline-upload behaviour.
        """
        if self.defer_uploads:
            self.current_buffer_size_bytes = 0
            return
        await super()._upload_file(file_name)

    def _build_file_reference(self) -> "FileReference | None":
        """Surface the writer-owned directory as an ephemeral FileReference.

        Returned in ``WriterResult.files`` only when ``defer_uploads=True``.
        Default mode returns ``None`` so the activity interceptor does not
        re-upload files that are already in the store from inline uploads.
        """
        if not self.defer_uploads:
            return None
        from application_sdk.contracts.types import FileReference  # noqa: PLC0415

        return FileReference.from_local(self.path)

    async def _write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a Parquet file, casting null-typed columns to string.

        When a pandas DataFrame has an all-null column, pyarrow infers the parquet
        type as ``null``. This causes silent data loss when daft reads multiple
        parquet files with mixed null/string column types — daft resolves the
        conflict by using ``null`` for ALL rows, dropping actual data from files
        that had the column typed as ``string``. See BLDX-837.
        """
        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow.parquet

        table = pa.Table.from_pandas(chunk, preserve_index=False)

        # Fast path: no null-typed columns → write directly (O(cols) check)
        if not any(pa.types.is_null(f.type) for f in table.schema):
            pq.write_table(table, file_name, compression="snappy")
            return

        # Slow path: cast null-typed columns to large_string before writing
        new_schema = pa.schema(
            [
                pa.field(f.name, pa.large_string()) if pa.types.is_null(f.type) else f
                for f in table.schema
            ]
        )
        table = table.cast(new_schema)
        pq.write_table(table, file_name, compression="snappy")
