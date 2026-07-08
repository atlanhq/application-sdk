import os
import warnings
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.common.file_ops import SafeFileOps
from application_sdk.common.types import DataframeType
from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import get_metrics
from application_sdk.storage.formats.utils import (
    JSON_FILE_EXTENSION,
    _download_files,
    path_gen,
)

if TYPE_CHECKING:
    import pandas as pd

from application_sdk.storage.formats import Reader, Writer

logger = get_logger(__name__)


class JsonFileReader(Reader):
    """JSON File Reader class to read data from JSON files using orjson.

    Supports reading both single files and directories containing multiple JSON files.
    Follows Python's file I/O pattern with read/close semantics and supports context managers.

    Both ``read()`` and ``read_batches()`` return ``pd.DataFrame`` — ``read()``
    materialises all records at once; ``read_batches()`` yields one
    ``pd.DataFrame`` per ``chunk_size`` records, streaming the file line-by-line.

    Attributes:
        path (str): Path to JSON file or directory containing JSON files.
        chunk_size (int): Number of rows per batch.
        file_names (Optional[List[str]]): List of specific file names to read.
        dataframe_type (DataframeType): Type of dataframe to return (pandas only;
            daft is a deprecated no-op alias that routes to the pandas path).
        cleanup_on_close (bool): Whether to clean up downloaded temp files on close.

    Example:
        Using context manager (recommended)::

            async with JsonFileReader(path="/data/input") as reader:
                df = await reader.read()
            # close() called automatically, temp files cleaned up

        Reading in batches::

            async with JsonFileReader(path="/data/input", chunk_size=50000) as reader:
                async for batch in reader.read_batches():
                    process(batch)

        Using close() explicitly::

            reader = JsonFileReader(path="/data/input")
            df = await reader.read()
            await reader.close()  # Clean up downloaded temp files
    """

    def __init__(
        self,
        path: str,
        file_names: list[str] | None = None,
        chunk_size: int | None = 100000,
        dataframe_type: DataframeType = DataframeType.pandas,
        cleanup_on_close: bool = True,
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): Path to JSON file or directory containing JSON files.
                It accepts both types of paths:
                local path or object store path
                Wildcards are not supported.
            file_names (Optional[List[str]]): List of specific file names to read. Defaults to None.
            chunk_size (int): Number of rows per batch. Defaults to 100000.
            dataframe_type (DataframeType): Type of dataframe to read. Defaults to DataframeType.pandas.
            cleanup_on_close (bool): Whether to clean up downloaded temp files on close. Defaults to True.

        Raises:
            ValueError: When path is not provided or when single file path is combined with file_names
        """
        warnings.warn(
            "JsonFileReader is deprecated and will be removed in v4.0. "
            "Migrate now: declare the upstream artifact as a FileReference "
            "field on your task's typed Input — the SDK's activity "
            "interceptor auto-materialises it to a local path before the "
            "task runs (with sha256 sidecar verification + parallel "
            "transfers), then read it directly with orjson / json. See "
            "docs/agents/coding-standards.md.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.extension = JSON_FILE_EXTENSION

        # Validate that single file path and file_names are not both specified
        if path.endswith(self.extension) and file_names:
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
        self.file_names = file_names
        self.dataframe_type = dataframe_type
        self.cleanup_on_close = cleanup_on_close

        if dataframe_type == DataframeType.daft:
            import warnings as _warnings  # noqa: PLC0415

            _warnings.warn(
                "DataframeType.daft is deprecated and will be removed in v4.0; "
                "use DataframeType.pandas. Routing to the pandas/orjson path.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.dataframe_type = DataframeType.pandas

    async def read(self) -> "pd.DataFrame":
        """Read the data from the JSON files and return as a single DataFrame.

        Returns:
            pd.DataFrame: Combined dataframe from JSON files.

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
        """Read the data from the JSON files as batched pandas DataFrames.

        Files are read line-by-line via orjson; each yielded batch is a
        ``pd.DataFrame`` of up to ``chunk_size`` rows. Peak memory is bounded
        to one batch at a time — the file never fully materialises.

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

    async def _get_batched_dataframe(
        self,
    ) -> AsyncIterator["pd.DataFrame"]:
        """Read data from JSON files line-by-line via orjson, yielding pd.DataFrame batches."""
        try:
            import pandas as pd  # noqa: PLC0415 — optional dep: pandas

            # Ensure files are available (local or downloaded)
            json_files = await _download_files(
                self.path, self.extension, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(json_files)
            logger.info("Reading %d JSON files in batches", len(json_files))

            chunk_size = self.chunk_size or 100000
            batch: list[dict] = []
            # Files must be JSONL (one JSON object per line). Multi-line JSON
            # arrays are not supported and will raise orjson.JSONDecodeError.
            for json_file in json_files:
                with open(json_file, "rb") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        batch.append(orjson.loads(line))
                        if len(batch) >= chunk_size:
                            yield pd.DataFrame(batch)
                            batch = []
            if batch:
                yield pd.DataFrame(batch)
        # conformance: ignore[E004] pure re-raise into typed FormatReadError; exception is not swallowed
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatReadError,
            )

            raise FormatReadError(cause=e) from e

    async def _get_dataframe(self) -> "pd.DataFrame":
        """Read data from JSON files using orjson line-by-line into a pandas DataFrame."""
        try:
            import pandas as pd  # noqa: PLC0415 — optional dep: pandas

            # Ensure files are available (local or downloaded)
            json_files = await _download_files(
                self.path, self.extension, self.file_names
            )
            # Track downloaded files for cleanup on close
            self._downloaded_files.extend(json_files)
            logger.info("Reading %d JSON files as pandas dataframe", len(json_files))

            # All records are accumulated in memory before building the
            # DataFrame. Suitable only for small datasets (≲ a few hundred MB).
            # For large inputs, use read_batches() to stay bounded.
            all_records: list[dict] = []
            for json_file in json_files:
                with open(json_file, "rb") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            all_records.append(orjson.loads(line))
            return pd.DataFrame(all_records)
        # conformance: ignore[E004] pure re-raise into typed FormatReadError; exception is not swallowed
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatReadError,
            )

            raise FormatReadError(cause=e) from e


class JsonFileWriter(Writer):
    """Output handler for writing data to JSON files.

    This class provides functionality for writing data to JSON files with support
    for chunking large datasets, buffering, and automatic file path generation.
    It accepts pandas DataFrames; DataframeType.daft is a deprecated no-op alias
    that routes to the pandas path.

    The output can be written to local files and optionally uploaded to an object
    store. Files are named using a configurable path generation scheme that
    includes chunk numbers for split files.

    Attributes:
        path (str): Full path where JSON files will be written.
        typename (Optional[str]): Type identifier for the data being written.
        chunk_start (Optional[int]): Starting index for chunk numbering.
        buffer_size (int): Size of the write buffer in bytes.
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks written.
        buffer (List[Union[pd.DataFrame, list]]): Buffer for accumulating
            data before writing.
    """

    def __init__(
        self,
        path: str,
        typename: str | None = None,
        chunk_start: int | None = None,
        buffer_size: int | None = 5000,
        chunk_size: int | None = 50000,  # to limit the memory usage on upload
        total_record_count: int | None = 0,
        chunk_count: int | None = 0,
        start_marker: str | None = None,
        end_marker: str | None = None,
        retain_local_copy: bool | None = False,
        dataframe_type: DataframeType = DataframeType.pandas,
        **kwargs: dict[str, Any],
    ):
        """Initialize the JSON output handler.

        Args:
            path (str): Full path where JSON files will be written.
            typename (Optional[str], optional): Type identifier for the data being written.
                If provided, a subdirectory with this name will be created under path.
                Defaults to None.
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
        # JsonFileWriter is on the v4.0 removal path. We surface a
        # DeprecationWarning here to push callers onto FileReference *now*
        # rather than waiting for v4.0 to break them — the new pattern is
        # already supported, fully optimised, and copy-paste documented.
        warnings.warn(
            "JsonFileWriter is deprecated and will be removed in v4.0. "
            "Migrate now: use application_sdk.storage.rolling.RollingFileWriter "
            "(time-based rollover, heartbeat-friendly) or write JSON locally "
            "(orjson.dumps + open(path, 'wb')) and return a FileReference "
            "for the output directory — the Temporal activity interceptor "
            "persists it with SHA-256 sidecars and parallel transfers, no "
            "caller-side upload code needed. See the 'Replacing "
            "ParquetFileWriter / JsonFileWriter' section in "
            "docs/agents/coding-standards.md.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.path = path
        self.typename = typename
        self.chunk_start = chunk_start
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.buffer_size = buffer_size
        self.chunk_size = chunk_size or 50000  # to limit the memory usage on upload
        self.buffer: list = []
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
        self._is_closed = False
        self._statistics = None

        if not self.path:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatPathRequiredError,
            )

            raise FormatPathRequiredError()

        if dataframe_type == DataframeType.daft:
            import warnings as _warnings  # noqa: PLC0415

            _warnings.warn(
                "DataframeType.daft is deprecated and will be removed in v4.0; "
                "use DataframeType.pandas. Routing to the pandas/orjson path.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.dataframe_type = DataframeType.pandas

        if typename:
            self.path = os.path.join(self.path, typename)
        SafeFileOps.makedirs(self.path, exist_ok=True)

        if self.chunk_start:
            self.chunk_count = self.chunk_start + self.chunk_count

    async def _write_chunk(self, chunk: "pd.DataFrame", file_name: str):
        """Write a chunk to a JSON file using orjson."""

        def _default_serializer(obj: object) -> str:
            # pandas.Timestamp and similar datetime-like objects are not
            # natively handled by orjson when they appear as dict values.
            return str(obj)

        mode = "ab+" if SafeFileOps.exists(file_name) else "wb"
        with SafeFileOps.open(file_name, mode=mode) as f:
            for record in chunk.to_dict(orient="records"):
                f.write(
                    orjson.dumps(
                        record,
                        default=_default_serializer,
                        option=orjson.OPT_APPEND_NEWLINE,
                    )
                )

    async def _finalize(self) -> None:
        """Finalize the JSON writer before closing.

        Uploads any remaining buffered data to the object store.
        Only updates chunk_count/partitions when it performs the upload, to
        avoid double-counting with _write_chunk which already updates statistics
        after its own upload.
        """
        if self.current_buffer_size_bytes > 0:
            output_file_name = f"{self.path}/{path_gen(self.chunk_count, self.chunk_part, self.start_marker, self.end_marker, extension=self.extension)}"
            await self._upload_file(output_file_name)
            self.chunk_part += 1
            if self.chunk_start is None:
                self.chunk_count += 1
            self.partitions.append(self.chunk_part)
