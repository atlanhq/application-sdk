import inspect
import os
import threading
import uuid
import warnings
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from typing import TYPE_CHECKING, cast

from application_sdk._runtime.offload import run_in_thread, submit_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.common.atomic import atomic_path
from application_sdk.common.file_ops import SafeFileOps
from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.contracts.types import FileReference
from application_sdk.errors import AppError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.storage.batch import delete_prefix as _delete_prefix
from application_sdk.storage.errors import ObjectStoreNotProvidedError
from application_sdk.storage.formats import DataframeType, Reader, Writer
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
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

#: Directory under a writer's private scratch root holding accumulation chunks
#: until they are consolidated — see
#: :meth:`ParquetFileWriter._get_temp_base_path`.
_TEMP_ACCUMULATION_DIRNAME = "temp_accumulation"


class _ThreadConfinedParquetReader:
    """Owns one ``ParquetFile`` handle, touched only from worker threads.

    :meth:`ParquetFileReader._get_batched_dataframe` offloads every decode step
    to a worker thread and ``await``\\ s it, so every step is a cancellation
    point — and a worker thread cannot be killed (ADR-0010). Closing the handle
    from the event loop therefore raced an orphaned worker still inside
    ``next(batches)``, closing a handle pyarrow was decoding from (FND-315).

    Two rules remove that race:

    * The handle is opened, decoded from and closed **only** under
      :attr:`_lock`, so a close issued while a decode is in flight waits for it
      instead of landing underneath it. The lock is held for a whole decode, so
      it must never be acquired from the event loop.
    * The event loop never *awaits* the close — see
      :func:`~application_sdk._runtime.offload.submit_in_thread`. That is
      what keeps the original reason the close was never offloaded intact: an
      ``await`` in a ``finally`` can itself be cancelled, leaking the handle.
    """

    def __init__(self, file_path: str, chunk_size: int) -> None:
        self._file_path = file_path
        self._chunk_size = chunk_size
        self._lock = threading.Lock()
        self._parquet_file: "pq.ParquetFile | None" = None
        self._batches: "Iterator[pa.RecordBatch] | None" = None
        self._closed = False

    def next_frame(self) -> "pd.DataFrame | None":
        """Open the file on first call, then decode one batch. Worker threads only.

        Returns ``None`` when the file is exhausted, or when the handle is
        already closed — a step can still be queued behind the close a cancelled
        caller submitted.
        """
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow.parquet

        with self._lock:
            if self._closed:
                return None
            if self._batches is None:
                # Bound locally so the batch iterator is derived from the
                # inferred type, not the deferred-import attribute annotation
                # (which pyright cannot resolve at class scope).
                parquet_file = pq.ParquetFile(self._file_path)
                self._parquet_file = parquet_file
                self._batches = parquet_file.iter_batches(batch_size=self._chunk_size)
            batch = next(self._batches, None)
            return None if batch is None else batch.to_pandas()

    def close(self) -> None:
        """Close the handle once no decode is in flight. Worker threads only.

        Idempotent, and blocking: it waits on :attr:`_lock` for however long the
        in-flight decode takes.
        """
        with self._lock:
            self._closed = True
            parquet_file, self._parquet_file, self._batches = (
                self._parquet_file,
                None,
                None,
            )
            if parquet_file is not None:
                parquet_file.close()


def _normalize_all_null_string_columns(table: "pa.Table") -> "pa.Table":
    """Rewrite all-null ``string``/``large_string`` columns as Arrow ``null``.

    A shard written before CNCT-80 persisted an all-null column as
    ``large_string``. ``pa.concat_tables(..., promote_options="permissive")``
    can promote ``null`` to any concrete sibling type, but cannot reconcile
    ``large_string`` against a numeric sibling — so a prefix that mixes a
    legacy all-null shard with a new numeric shard still fails to merge.
    Rewriting the all-null string column as ``null`` restores that promotion
    without touching columns that actually hold string data.
    """
    import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

    if not isinstance(table, pa.Table):
        return table

    fields = []
    columns = []
    changed = False
    for i, field in enumerate(table.schema):
        column = table.column(i)
        if (
            pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
        ) and column.null_count == table.num_rows:
            # null_count == num_rows already holds for an empty (0-row) string
            # column, so legacy 0-row shards normalize here as well.
            fields.append(field.with_type(pa.null()))
            columns.append(pa.chunked_array([pa.nulls(table.num_rows, type=pa.null())]))
            changed = True
        else:
            fields.append(field)
            columns.append(column)

    if not changed:
        return table
    return pa.Table.from_arrays(
        columns, schema=pa.schema(fields, metadata=table.schema.metadata)
    )


class ParquetFileReader(Reader):
    """Parquet File Reader class to read data from Parquet files using pyarrow and pandas.

    Supports reading both single files and directories containing multiple parquet files.
    Follows Python's file I/O pattern with read/close semantics and supports context managers.

    Attributes:
        path (str): Path to parquet file or directory containing parquet files.
        chunk_size (int): Number of rows per batch.
        buffer_size (int): Number of rows per batch.
        file_names (Optional[List[str]]): List of specific file names to read.
        dataframe_type (DataframeType): Type of dataframe to return (pandas only; daft is deprecated).
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
            "Migrate now: declare the upstream artifact as a FileReference "
            "field on your task's typed Input — the SDK's activity "
            "interceptor auto-materialises it to a local path before the "
            "task runs (with sha256 sidecar verification + parallel "
            "transfers), then read it directly with pandas.read_parquet. "
            "See docs/agents/coding-standards.md.",
            DeprecationWarning,
            stacklevel=2,
        )

        # Validate that single file path and file_names are not both specified
        if path.endswith(PARQUET_FILE_EXTENSION) and file_names:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                SingleFilePathWithFileNamesError,
            )

            raise SingleFilePathWithFileNamesError(path=path)

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

        if dataframe_type == DataframeType.daft:
            import warnings as _warnings  # noqa: PLC0415

            _warnings.warn(
                "DataframeType.daft is deprecated and will be removed in v4.0; "
                "use DataframeType.pandas. Routing to the pandas/pyarrow path.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.dataframe_type = DataframeType.pandas

    async def read(self) -> "pd.DataFrame":
        """Read the data from the parquet files and return as a single DataFrame.

        Returns:
            pd.DataFrame: Combined dataframe from parquet files.

        Raises:
            ValueError: If the reader has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                ReaderClosedError,
            )

            raise ReaderClosedError()

        if self.dataframe_type == DataframeType.pandas:
            return await self._get_dataframe()
        else:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                UnsupportedDataframeTypeError,
            )

            raise UnsupportedDataframeTypeError(observed_type=str(self.dataframe_type))

    def read_batches(
        self,
    ) -> AsyncIterator["pd.DataFrame"]:
        """Read the data from the parquet files as batches of pandas DataFrames.

        Each yielded batch is a ``pd.DataFrame`` of up to ``chunk_size`` rows,
        streamed one pyarrow row-group at a time via
        ``pyarrow.parquet.ParquetFile.iter_batches()``.

        Note: ``JsonFileReader.read_batches()`` also yields ``pd.DataFrame``
        objects, so polymorphic consumers can treat both readers uniformly.

        Returns:
            AsyncIterator[pd.DataFrame]: Async iterator of pandas DataFrames.

        Raises:
            ValueError: If the reader has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                ReaderClosedError,
            )

            raise ReaderClosedError()

        if self.dataframe_type == DataframeType.pandas:
            return self._get_batched_dataframe()
        else:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                UnsupportedDataframeTypeError,
            )

            raise UnsupportedDataframeTypeError(observed_type=str(self.dataframe_type))

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
            import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow
            import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow.parquet

            # Ensure files are available (local or downloaded)
            parquet_files = await _download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info("Reading %d parquet files", len(parquet_files))

            def _read_and_combine() -> "pd.DataFrame":
                import pandas as pd  # noqa: PLC0415 — optional dep: pandas

                tables = [pq.read_table(f) for f in parquet_files]
                if not tables:
                    return pd.DataFrame()
                # Normalize legacy all-null large_string shards (pre-CNCT-80
                # writes) so permissive promotion can merge them against a
                # sibling shard's concrete numeric type.
                tables = [_normalize_all_null_string_columns(t) for t in tables]
                combined = pa.concat_tables(tables, promote_options="permissive")
                return combined.to_pandas()

            # Offloaded as one hop: reading every shard, promoting schemas and
            # materialising a single pandas frame is blocking disk I/O plus
            # CPU-bound Arrow work whose cost scales with the prefix. Inline it
            # holds the event loop for that whole span, which starves the
            # auto-heartbeat and gets a healthy activity killed (ADR-0010).
            return await run_in_thread(_read_and_combine)
        # An already-typed AppError carries its own category/audience/evidence
        # (e.g. ObjectStoreReadError -> DEPENDENCY_UNAVAILABLE + the searched
        # prefix). Re-wrapping it as FormatReadError would downgrade that to
        # INTERNAL/APP_OWNER and drop the evidence fields, so let it through
        # unchanged — the same guard `_download_files` already applies one
        # frame down for exactly this reason.
        except AppError:
            raise
        # conformance: ignore[E004] exception is re-raised as FormatReadError; traceback preserved in cause chain
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatReadError,
            )

            raise FormatReadError(cause=e) from e

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
            # Ensure files are available (local or downloaded)
            parquet_files = await _download_files(
                self.path, PARQUET_FILE_EXTENSION, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(parquet_files)
            logger.info("Reading %d parquet files in batches", len(parquet_files))

            chunk_size = self.chunk_size or 100000

            for parquet_file in parquet_files:
                # Opening reads the file footer; advancing the iterator reads
                # and decodes a whole row group and converts it to pandas.
                # Both are blocking, and at batch_size=100k the per-batch cost
                # is far past the heartbeat interval — offload each step so the
                # loop stays free to beat between batches (ADR-0010).
                #
                # The handle lives in the worker rather than in this frame:
                # every ``await`` below is a cancellation point, and a worker
                # thread cannot be killed, so a close issued from here could
                # land while an orphan was still decoding (FND-315).
                reader = _ThreadConfinedParquetReader(parquet_file, chunk_size)
                try:
                    while True:
                        frame = await run_in_thread(reader.next_frame)
                        if frame is None:
                            break
                        yield frame
                finally:
                    # Submitted, not awaited: an ``await`` in a ``finally`` is
                    # itself cancellable and would leak the handle, and the
                    # close must be free to wait out an in-flight decode
                    # without blocking the event loop for its duration.
                    submit_in_thread(reader.close)
        # See _get_dataframe: preserve an already-typed AppError.
        except AppError:
            raise
        # conformance: ignore[E004] exception is re-raised as FormatReadError; traceback preserved in cause chain
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatReadError,
            )

            raise FormatReadError(cause=e) from e


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
        # ParquetFileWriter is on the v4.0 removal path. We surface a
        # DeprecationWarning here to push callers onto FileReference *now*
        # rather than waiting for v4.0 to break them — the new pattern is
        # already supported, fully optimised, and copy-paste documented.
        warnings.warn(
            "ParquetFileWriter is deprecated and will be removed in v4.0. "
            "Migrate now: use application_sdk.storage.rolling.RollingFileWriter "
            "(time-based rollover, heartbeat-friendly) or write parquet "
            "locally and return a FileReference for the output directory — "
            "the Temporal activity interceptor persists it with SHA-256 "
            "sidecars and parallel transfers, no caller-side upload code "
            "needed. See the 'Replacing ParquetFileWriter / JsonFileWriter' "
            "section in docs/agents/coding-standards.md.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.extension = PARQUET_FILE_EXTENSION
        self.path = path
        self.typename = typename
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.buffer: list = []
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

        if dataframe_type == DataframeType.daft:
            import warnings as _warnings  # noqa: PLC0415

            _warnings.warn(
                "DataframeType.daft is deprecated and will be removed in v4.0; "
                "use DataframeType.pandas. Routing to the pandas/pyarrow path.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.dataframe_type = DataframeType.pandas

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
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatPathRequiredError,
            )

            raise FormatPathRequiredError()
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
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                ReplacePrefixEmptyError,
            )

            raise ReplacePrefixEmptyError()

        try:
            deleted_count = await _delete_prefix(self.path)
        except ObjectStoreNotProvidedError:
            logger.warning(
                "No object store configured, skipping prefix replacement — "
                "existing objects under %s were not deleted",
                normalized_prefix,
                exc_info=True,
            )
        else:
            # conformance: ignore[L018] ParquetFileWriter is deprecated (removed in v4.0); existing dashboards/alerts likely query these kwarg keys directly out of the JSON blob, so we keep the anti-pattern rather than risk breaking them for a class on its way out
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

        # conformance: ignore[E004] immediate cleanup-then-bare-reraise loses no information (the original exception and traceback propagate unchanged); a log call here would be the exact log-before-raise duplicate L009 avoids
        except Exception:
            await self._cleanup_temp_folders()  # Cleanup on error
            raise

    def get_full_path(self) -> str:
        """Get the full path of the output file.

        Returns:
            str: The full path of the output file.
        """
        return self.path

    # Consolidation helper methods

    def _get_temp_base_path(self) -> str:
        """Root of this writer's accumulation tree.

        Lives under the writer's private scratch root, so no two writers — and
        therefore no two activity attempts — can ever share an accumulation
        directory.

        That sharing was FND-315: every part of the old path was
        attempt-independent (``<output>/temp_accumulation/folder-{i}``, with
        ``temp_folder_index`` reset to 0 on each writer), so a retry landed on
        the exact directory an orphaned ``_convert_and_write`` worker was still
        writing into. The orphan's files shifted the retry's ``chunk-{n}``
        numbering — mixing partial output into a consolidation, or skipping the
        retry's own — and the retry's cleanup could ``rmtree`` a live
        directory.

        Being under the *scratch* root rather than the staged output root is
        FND-317: a folder a failed cleanup leaves behind can never be published
        as output or swept into a ``FileReference``.
        """
        return os.path.join(self._scratch_root, _TEMP_ACCUMULATION_DIRNAME)

    def _get_temp_folder_path(self, folder_index: int) -> str:
        """Generate temp folder path consistent with existing structure."""
        return os.path.join(self._get_temp_base_path(), f"folder-{folder_index}")

    def _get_consolidated_file_path(self, folder_index: int, chunk_part: int) -> str:
        """Staged path for a consolidated file, under its published name.

        The name is the published one; only the directory is private until
        ``close()`` publishes it (FND-317).
        """
        return os.path.join(
            self._write_root,
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
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                TempFolderPathMissingError,
            )

            raise TempFolderPathMissingError()

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

        # The consolidation path never reaches Writer._flush_buffer, so it
        # needs its own chunk boundary: accumulation can run for the whole
        # `write_batches` stream before the first consolidation happens.
        current_progress_tracker().mark_progress("writer.accumulate_chunk")

    async def _consolidate_current_folder(self):
        """Consolidate current temp folder using pyarrow."""
        if self.current_folder_records == 0 or self.current_temp_folder_path is None:
            return

        try:
            import pandas as pd  # noqa: PLC0415 — optional dep: pandas

            # Read all parquet files in temp folder
            temp_files = [
                os.path.join(self.current_temp_folder_path, f)
                for f in SafeFileOps.listdir(self.current_temp_folder_path)
                if f.endswith(self.extension)
            ]
            if not temp_files:
                return

            # Offloaded: a consolidation folder holds a full buffer's worth of
            # chunks, so reading them all back and concatenating is seconds of
            # blocking disk I/O plus CPU. Inline it starves the auto-heartbeat
            # for that span (ADR-0010). The in-memory size estimate rides the
            # same hop: it walks every column once.
            def _read_and_concat() -> "tuple[pd.DataFrame, int]":
                frame = pd.concat(
                    [pd.read_parquet(f) for f in temp_files], ignore_index=True
                )
                return frame, int(frame.memory_usage(deep=True).sum())

            combined_df, estimated_bytes = await run_in_thread(_read_and_concat)

            # One consolidated file per folder is the point of consolidating:
            # the folder *is* the chunk (``consolidation_threshold`` ==
            # ``chunk_size`` rows). Only ``max_file_size_bytes`` splits it, and
            # against the in-memory estimate — larger than the snappy parquet
            # that lands, so a split errs towards fewer rows, never a file over
            # the cap. This loop used to slice by ``buffer_size`` instead, so
            # every chunk became ``chunk_size / buffer_size`` files — twenty at
            # the defaults. A 93M-row run staged ~18,700 objects that way, and
            # its hand-off then paid one set of round trips per object
            # (FND-1339).
            total_rows = len(combined_df)
            bytes_per_row = max(1, estimated_bytes // max(1, total_rows))
            rows_per_file = max(
                1, min(total_rows, self.max_file_size_bytes // bytes_per_row)
            )
            partitions = 0
            chunk_part_start = 0
            for i in range(0, total_rows, rows_per_file):
                chunk = combined_df.iloc[i : i + rows_per_file]
                consolidated_file_path = self._get_consolidated_file_path(
                    folder_index=self.chunk_count,
                    chunk_part=chunk_part_start + partitions,
                )
                await self._write_chunk(chunk, consolidated_file_path)

                if not self.defer_uploads:
                    # The module-level upload, not `self._upload_file`: the
                    # consolidation path has always kept its local files
                    # regardless of `retain_local_copy`, and routing through
                    # the base method would start deleting them. The key is
                    # still the published one so staging stays invisible to
                    # the store (FND-317).
                    await _upload_file(
                        self._published_path(consolidated_file_path),
                        consolidated_file_path,
                    )
                partitions += 1

                # One consolidated file written (and uploaded) is one unit.
                # Marking inside the loop rather than after it matters: a
                # folder at the consolidation threshold can produce many files,
                # and a slow store would otherwise make the whole consolidation
                # one quiet window.
                current_progress_tracker().mark_progress("writer.consolidate_chunk")

            # Update statistics
            self.chunk_count += 1
            self.total_record_count += self.current_folder_records
            self.partitions.append(partitions)

            # Record metrics
            self.metrics.record_metric(
                name="consolidated_files",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pyarrow_consolidation"},
                description="Number of consolidated parquet files created",
            )

            logger.info(
                "Consolidated folder index=%d record_count=%d",
                self.temp_folder_index,
                self.current_folder_records,
            )

        except Exception:
            # conformance: ignore[L009] adds caller-invisible partial state (temp_folder_index being consolidated) not carried by the propagating exception
            logger.error(
                "Error consolidating folder %s",
                self.temp_folder_index,
                exc_info=True,
            )
            raise

    async def _cleanup_temp_folders(self):
        """Clean up this writer's temp folders after consolidation.

        Only ever removes paths under this writer's own token-scoped tree, so it
        cannot delete a directory an orphaned worker from a cancelled attempt is
        still writing into (FND-315).
        """
        try:
            # Add current folder to cleanup list if it exists
            if self.current_temp_folder_path is not None:
                self.temp_folders_created.append(self.temp_folder_index)

            # Clean up all temp folders
            for folder_index in self.temp_folders_created:
                temp_folder = self._get_temp_folder_path(folder_index)
                if not SafeFileOps.exists(temp_folder):
                    continue
                try:
                    # Offloaded: an accumulation folder holds a run's worth of
                    # parquet chunks, and rmtree on the event loop stalls every
                    # other coroutine — including the auto-heartbeat.
                    await run_in_thread(SafeFileOps.rmtree, temp_folder)
                except OSError:
                    # No `ignore_errors=True` any more. It was load-bearing only
                    # while attempts shared this tree, where a vanished
                    # directory meant a sibling attempt had deleted it — the
                    # very failure it hid (FND-315). Within a writer-scoped
                    # tree a failure here is a real one (full disk, permission
                    # change, a file still held open) and worth reporting.
                    logger.warning(
                        "Failed to remove parquet accumulation folder %s; "
                        "it will be left on local disk",
                        temp_folder,
                        exc_info=True,
                    )

            # Drop this writer's accumulation root once it is empty. No sibling
            # can be inside it — the whole tree is private to this writer — so
            # a failure here is this writer's own and worth surfacing.
            temp_base = self._get_temp_base_path()
            if SafeFileOps.exists(temp_base) and not SafeFileOps.listdir(temp_base):
                try:
                    SafeFileOps.rmdir(temp_base)
                except OSError:
                    logger.warning(
                        "Failed to remove parquet accumulation root %s; "
                        "it will be left on local disk until the writer closes",
                        temp_base,
                        exc_info=True,
                    )

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
            output_file_name = os.path.join(
                self._write_root,
                path_gen(self.chunk_count, chunk_part, extension=self.extension),
            )
            if os.path.exists(output_file_name):
                try:
                    await self._upload_file(output_file_name)
                except ObjectStoreNotProvidedError:
                    # Local dev with no object store configured. Any other
                    # exception (transient blob upload failure, auth token
                    # rotation, network error) must propagate: the base
                    # _flush_buffer has already incremented
                    # total_record_count, and swallowing here would make the
                    # writer report more rows in statistics.json than
                    # actually reached object storage. Mirrors the safe
                    # pattern used in _ensure_prefix_replaced above.
                    logger.warning(
                        "No object store configured, skipping upload — %s "
                        "was written locally only and will not reach object storage",
                        output_file_name,
                        exc_info=True,
                    )
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

        Called after ``close()`` has published. The reference walks the whole
        output directory, so it covers this writer's own published output plus
        anything already in that directory from another writer — staging
        guarantees only that *this* writer's staging tree is never adopted
        (FND-317), not that a shared output directory holds this writer alone.
        """
        if not self.defer_uploads:
            return None
        return FileReference.from_local(self.path)

    async def _write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a Parquet file.

        An all-null column is written with its natural pyarrow-inferred
        ``null`` type rather than cast to ``large_string``. The cast was a
        workaround for a daft merge bug (BLDX-837) that no longer applies —
        daft was removed entirely in #2300. Leaving the column typed ``null``
        lets the reader's ``pa.concat_tables(..., promote_options="permissive")``
        promote it against a sibling shard's real type at merge time; casting
        it to ``large_string`` instead made that promotion impossible whenever
        a sibling shard was typed e.g. ``double`` (CNCT-80).
        """
        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow.parquet

        # Offloaded as one hop: `Table.from_pandas` is CPU-bound conversion over
        # the whole chunk and `pq.write_table` is blocking disk I/O. Either one
        # inline stalls the event loop — including the auto-heartbeat — for its
        # full duration on large chunks (ADR-0010).
        def _convert_and_write() -> None:
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            row_group_size = max(
                1,
                min(
                    len(table), 16_000_000 // max(1, table.nbytes // max(1, len(table)))
                ),
            )
            # Staged and renamed rather than written in place (FND-318).
            # `pq.write_table` always overwrites its target, so this chunk is a
            # whole-file write and can be made atomic without changing what
            # lands: a chunk that runs out of disk leaves no file at
            # `file_name` at all, rather than a parquet footer-less prefix that
            # every downstream reader fails on identically. `table.nbytes` is
            # the pre-compression size, so it over-estimates what snappy will
            # actually need — deliberately, since the preflight is only meant
            # to catch the plainly impossible write.
            with atomic_path(
                file_name,
                operation="parquet chunk write",
                required_bytes=table.nbytes or None,
            ) as staging:
                pq.write_table(
                    table,
                    str(staging),
                    compression="snappy",
                    row_group_size=row_group_size,
                )

        await run_in_thread(_convert_and_write)
