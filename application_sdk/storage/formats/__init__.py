"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import errno
import gc
import inspect
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Union, cast

import orjson

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.common._listing import WRITER_STAGING_DIRNAME, prune_internal_dirs
from application_sdk.common.atomic import atomic_copy, atomic_write, disk_full_guard
from application_sdk.common.models import TaskStatistics
from application_sdk.common.types import DataframeType
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType
from application_sdk.storage.formats.utils import (
    estimate_dataframe_record_size,
    is_empty_dataframe,
    path_gen,
)
from application_sdk.storage.ops import upload_file as _upload_file


@dataclass
class WriterResult(TaskStatistics):
    """Outcome of a Writer.close() call.

    Subclasses ``TaskStatistics`` so existing callers that read
    ``result.total_record_count``, ``result.chunk_count``, ``result.partitions``,
    ``result.typename`` keep working unchanged via inheritance.

    Adds one new field — ``files`` — for callers who opted into the deferred-
    upload contract via ``defer_uploads=True`` on the writer constructor.
    When deferred uploads are off (the default), ``files`` is ``None`` because
    files have already been uploaded inline and surfacing a ``FileReference``
    would risk a double-upload through the activity interceptor.

    Apps that want SHA-256 dedup, integrity verification, and parallel
    transfers via the ``FileReference`` boundary set ``defer_uploads=True``
    and read ``result.files`` here. Apps that don't care can ignore this
    field entirely — their existing code paths are unaffected.
    """

    files: FileReference | None = None


logger = get_logger(__name__)

#: Directory holding every writer's private staging tree. Created as a *sibling*
#: of the writer's output directory, never inside it — see
#: :meth:`Writer._ensure_staging_root`.
#:
#: The name itself lives in ``common._listing`` alongside every other
#: SDK-working-directory name, because being a sibling only puts this tree out
#: of reach of a walk of the *output* directory — a prefix upload of the run
#: root walks a level higher and would ship a cancelled attempt's staging tree
#: as run output. One definition, so the walkers and this module cannot
#: disagree about what is an artifact (FND-318).
_STAGING_ROOT_DIRNAME = WRITER_STAGING_DIRNAME

#: Subdirectory of a writer's staging root holding files bound for the output
#: directory. Its layout mirrors the output directory exactly, so publishing is
#: a relative-path-preserving move.
_STAGED_OUTPUT_DIRNAME = "output"

#: Subdirectory of a writer's staging root holding intermediates that are never
#: published — parquet's accumulation tree is the only current occupant.
_STAGED_SCRATCH_DIRNAME = "scratch"


if TYPE_CHECKING:
    import pandas as pd


class Reader(ABC):
    """Abstract base class for reader data sources.

    This class defines the interface for reader handlers that can read data
    from various sources in different formats. Follows Python's file I/O
    pattern with read/close semantics and supports context managers.

    Attributes:
        path (str): Path where the reader will read from.
        _is_closed (bool): Whether the reader has been closed.
        _downloaded_files (List[str]): List of downloaded temporary files to clean up.
        cleanup_on_close (bool): Whether to clean up downloaded temp files on close.

    Example:
        Using close() explicitly::

            reader = ParquetFileReader(path="/data/input")
            df = await reader.read()
            await reader.close()  # Cleans up any downloaded temp files

        Using context manager (recommended)::

            async with ParquetFileReader(path="/data/input") as reader:
                df = await reader.read()
            # close() called automatically

        Reading in batches with context manager::

            async with JsonFileReader(path="/data/input") as reader:
                async for batch in reader.read_batches():
                    process(batch)
            # close() called automatically
    """

    path: str
    _is_closed: bool
    _downloaded_files: list[str]
    cleanup_on_close: bool = True

    def __init__(self) -> None:
        """Initialize per-instance mutable state.

        Subclasses that override ``__init__`` should call ``super().__init__()``
        to ensure ``_downloaded_files`` and ``_is_closed`` are not shared across
        instances via class-level mutable defaults.
        """
        self._downloaded_files: list[str] = []
        self._is_closed: bool = False

    async def __aenter__(self) -> "Reader":
        """Enter the async context manager.

        Returns:
            Reader: The reader instance.
        """
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the async context manager, closing the reader.

        Args:
            exc_type: Exception type if an exception was raised.
            exc_val: Exception value if an exception was raised.
            exc_tb: Exception traceback if an exception was raised.
        """
        await self.close()

    async def close(self) -> None:
        """Close the reader and clean up any downloaded temporary files.

        This method cleans up any temporary files that were downloaded from
        the object store during read operations. Calling close() multiple
        times is safe (subsequent calls are no-ops).

        Note:
            Set ``cleanup_on_close=False`` during initialization to retain
            downloaded files after closing.

        Example::

            reader = ParquetFileReader(path="/data/input")
            df = await reader.read()
            await reader.close()  # Cleans up temp files
        """
        if self._is_closed:
            return

        if self.cleanup_on_close and self._downloaded_files:
            await self._cleanup_downloaded_files()

        self._is_closed = True

    async def _cleanup_downloaded_files(self) -> None:
        """Clean up downloaded temporary files.

        Override this method in subclasses for custom cleanup behavior.
        """
        for file_path in self._downloaded_files:
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                elif os.path.isdir(file_path):
                    # Offloaded: a downloaded prefix is an unbounded tree, and
                    # rmtree on the event loop stalls every other coroutine —
                    # including a @task's auto-heartbeat — for its duration.
                    await run_in_thread(shutil.rmtree, file_path, ignore_errors=True)
            except Exception:
                logger.warning(
                    "Failed to clean up temporary file: %s",
                    file_path,
                    exc_info=True,
                )

        self._downloaded_files.clear()

    @abstractmethod
    def read_batches(
        self,
    ) -> (
        Iterator["pd.DataFrame"]
        | AsyncIterator["pd.DataFrame"]
        | AsyncIterator[list[dict]]
    ):
        """Get an iterator of batched pandas DataFrames (or list[dict] batches).

        Returns:
            Iterator["pd.DataFrame"]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
            ValueError: If the reader has been closed.
        """
        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            AbstractFormatReaderError,
        )

        raise AbstractFormatReaderError()

    @abstractmethod
    async def read(self) -> "pd.DataFrame":
        """Get a single pandas DataFrame.

        Returns:
            "pd.DataFrame": A pandas DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
            ValueError: If the reader has been closed.
        """
        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            AbstractFormatReaderError,
        )

        raise AbstractFormatReaderError()


class WriteMode(Enum):
    """Enumeration of write modes for output operations."""

    APPEND = "append"
    OVERWRITE = "overwrite"
    OVERWRITE_PARTITIONS = "overwrite-partitions"


class Writer(ABC):
    """Abstract base class for writer handlers.

    This class defines the interface for writer handlers that can write data
    to various destinations in different formats. Follows Python's file I/O
    pattern with open/write/close semantics and supports context managers.

    Attributes:
        path (str): Path where the writer will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks the writer was split into.
        buffer_size (int): Size of the buffer to write data to.
        max_file_size_bytes (int): Maximum size of the file to write data to.
        current_buffer_size (int): Current size of the buffer to write data to.
        current_buffer_size_bytes (int): Current size of the buffer to write data to.
        partitions (List[int]): Partitions of the writer.

    Example:
        Using close() explicitly::

            writer = ParquetFileWriter(path="/data/output", typename="users")
            await writer.write(dataframe)
            result = await writer.close()
            # result.statistics → TaskStatistics
            # result.files      → ephemeral FileReference for the output dir

        Using context manager (recommended)::

            async with ParquetFileWriter(path=base, typename="users") as w:
                await w.write(dataframe)
            # close() called automatically; final result retrievable via
            # w.last_result if needed.
    """

    path: str
    output_prefix: str
    total_record_count: int
    chunk_count: int
    chunk_part: int
    buffer_size: int
    max_file_size_bytes: int
    current_buffer_size: int
    current_buffer_size_bytes: int
    partitions: list[int]
    extension: str
    dataframe_type: DataframeType
    _is_closed: bool = False
    _statistics: TaskStatistics | None = None
    _result: "WriterResult | None" = None
    #: Lazily created on first write; see :meth:`_ensure_staging_root`. Declared
    #: as a class-level default so a third-party ``Writer`` subclass that does
    #: not call a base ``__init__`` still resolves it — assignment below shadows
    #: it per instance.
    _staging_root: str | None = None
    _staging_published: bool = False

    # ── Staging (FND-317) ─────────────────────────────────────────────────
    #
    # A writer never writes into its output directory. It writes into a private
    # staging tree and publishes into the output directory in one uninterrupted
    # step at ``close()``.
    #
    # The reason is the window FND-315 established. Cancelling an activity
    # leaves an orphaned ``run_in_thread`` worker that cannot be killed and is
    # still inside its write, holding a path it resolved *before* the cancel.
    # Every part of the old output path was attempt-independent — ``self.path``
    # is the same directory for every attempt whenever ``typename`` is set, and
    # both ``chunk_count`` and ``chunk_part`` restart from ``0`` — so a retry
    # resolved the exact filename the orphan was mid-write into. Two failures
    # followed: the orphan's late write replaced the retry's file, and (worse,
    # because it needs no filename collision at all) ``_build_file_reference``
    # listed the output directory, so the interceptor uploaded the orphan's
    # non-colliding files as part of the retry's output — duplicate rows, with
    # ``statistics.json`` reporting only the retry's own count.
    #
    # Staging closes both: an orphan's files land in the orphan's own tree, and
    # only a writer that reaches ``close()`` publishes. The published names are
    # untouched — they are a downstream contract, which is why FND-315's fix
    # (suffix the path with a per-writer token) could not simply be extended
    # from the private temp tree to the output directory.

    def _ensure_staging_root(self) -> str:
        """Create this writer's private staging tree on first use.

        Sited as a sibling of the output directory rather than inside it, for
        two reasons: the same parent means the same filesystem, so publishing
        is an atomic ``os.replace`` per file rather than a copy; and a
        directory ``FileReference`` walks its tree recursively, so anything
        left under ``self.path`` by a cancelled attempt would be uploaded as
        part of the next attempt's output — the very failure being fixed.

        A successful ``close()`` removes the tree. What a cancelled attempt
        leaves behind stays next to the output directory rather than inside
        it, which is the point: it is inert there.
        """
        root = self._staging_root
        if root is None:
            # normpath first: a caller-supplied trailing separator would
            # otherwise make dirname() return self.path itself, siting the
            # staging tree *inside* the output directory.
            parent = os.path.dirname(os.path.normpath(self.path)) or "."
            # Full hex, not a truncated slice: a shorter token plus the
            # non-exclusive makedirs below could collide two writers into one
            # staging tree. 128 bits makes that a non-event.
            root = os.path.join(parent, _STAGING_ROOT_DIRNAME, uuid.uuid4().hex)
            os.makedirs(os.path.join(root, _STAGED_OUTPUT_DIRNAME), exist_ok=True)
            os.makedirs(os.path.join(root, _STAGED_SCRATCH_DIRNAME), exist_ok=True)
            self._staging_root = root
        return root

    @property
    def _write_root(self) -> str:
        """Directory this writer writes output files into before publishing.

        Mirrors the layout of :attr:`path`: a file staged at
        ``<_write_root>/<relative>`` publishes to ``<path>/<relative>`` and
        uploads under that same key, so chunk filenames stay exactly what
        downstream consumers already read.
        """
        return os.path.join(self._ensure_staging_root(), _STAGED_OUTPUT_DIRNAME)

    @property
    def _scratch_root(self) -> str:
        """Directory for intermediates that must never reach the output.

        Distinct from :attr:`_write_root` because publishing moves the whole
        staged-output tree — anything a subclass leaves here after a failed
        cleanup would otherwise be published alongside real chunks.
        """
        return os.path.join(self._ensure_staging_root(), _STAGED_SCRATCH_DIRNAME)

    def _published_path(self, staged_path: str) -> str:
        """Path — and object-store key — that *staged_path* publishes to.

        A path that is not under :attr:`_write_root` is returned unchanged, so
        a subclass or caller that hands over an already-published path keeps
        working.
        """
        relative = os.path.relpath(staged_path, self._write_root)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return staged_path
        return os.path.normpath(os.path.join(self.path, relative))

    def _publish_staged_files(self) -> None:
        """Move this writer's staged output into its output directory.

        Synchronous on purpose, and the one place in the writer where that is
        the point rather than an oversight: there is no ``await`` between "the
        files are staged" and "the files are published", so a cancellation can
        never leave a half-published attempt for the next one to adopt. The
        cost is a burst of same-filesystem renames — metadata operations, no
        bytes moved — at the end of a writer's life.

        SDK working directories under the staged tree are not descended into.
        The writers below stage each individual file too (FND-318), so
        ``.sdk-partial`` sits inside this tree by construction — publishing it
        would recreate the directory in the output and, if an atomic write had
        failed, hand a partial file the very name this staging exists to
        withhold.
        """
        if self._staging_root is None or self._staging_published:
            return

        staged_output = os.path.join(self._staging_root, _STAGED_OUTPUT_DIRNAME)
        copied = 0
        for directory, subdirectories, file_names in os.walk(staged_output):
            prune_internal_dirs(subdirectories)
            destination_dir = os.path.normpath(
                os.path.join(self.path, os.path.relpath(directory, staged_output))
            )
            os.makedirs(destination_dir, exist_ok=True)
            for file_name in file_names:
                source = os.path.join(directory, file_name)
                destination = os.path.join(destination_dir, file_name)
                try:
                    os.replace(source, destination)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    # Staging is a sibling of the output directory, so this only
                    # happens when the output directory is itself a mount point.
                    # Copy rather than fail — but never straight to the final
                    # name: a copy interrupted there leaves a truncated file at
                    # the artifact's real path, the exact incident class this
                    # staging exists to kill. atomic_copy stages the copy next
                    # to the destination and publishes it with a rename, so the
                    # final name only ever appears complete; the source unlink
                    # after a successful copy completes the move.
                    atomic_copy(source, destination, operation="writer publish")
                    os.unlink(source)
                    copied += 1

        if copied:
            # Reported once for the whole publish, not per file: the fact worth
            # knowing is that this deployment's layout turns a metadata-only
            # publish into real byte copying.
            logger.warning(
                "Published %d file(s) by copying: staging and %s are on "
                "different filesystems, so the rename this step relies on "
                "could not be used",
                copied,
                self.path,
            )

        self._staging_published = True
        shutil.rmtree(self._staging_root, ignore_errors=True)

    async def __aenter__(self) -> "Writer":
        """Enter the async context manager.

        Returns:
            Writer: The writer instance.
        """
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the async context manager, closing the writer.

        Args:
            exc_type: Exception type if an exception was raised.
            exc_val: Exception value if an exception was raised.
            exc_tb: Exception traceback if an exception was raised.
        """
        await self.close()

    def _convert_to_dataframe(
        self,
        data: Union["pd.DataFrame", dict[str, Any], list[dict[str, Any]]],
    ) -> "pd.DataFrame":
        """Convert input data to a DataFrame if needed.

        Args:
            data: Input data - can be a pandas DataFrame, dict, or list of dicts.

        Returns:
            A pandas DataFrame.

        Raises:
            UnsupportedDataTypeError: If data type is not supported.
        """
        import pandas as pd  # noqa: PLC0415 — optional dep: pandas

        # Already a pandas DataFrame - return as-is
        if isinstance(data, pd.DataFrame):
            return data

        # Convert dict or list of dicts to DataFrame
        if isinstance(data, dict) or (
            isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict)
        ):
            return pd.DataFrame([data] if isinstance(data, dict) else data)

        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            UnsupportedDataTypeError,
        )

        raise UnsupportedDataTypeError(observed_type=type(data).__name__)

    async def write(
        self,
        data: Union["pd.DataFrame", dict[str, Any], list[dict[str, Any]]],
        **kwargs: Any,
    ) -> None:
        """Write data to the output destination.

        Supports writing DataFrames, dicts (converted to single-row DataFrame),
        or lists of dicts (converted to multi-row DataFrame).

        Args:
            data: Data to write - DataFrame, dict, or list of dicts.
            **kwargs: Additional parameters passed to the underlying write method.

        Raises:
            ValueError: If the writer has been closed or dataframe_type is unsupported.
            TypeError: If data type is not supported.
        """
        if self._is_closed:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                WriterClosedError,
            )

            raise WriterClosedError()

        # Convert to DataFrame if needed
        dataframe = self._convert_to_dataframe(data)

        if self.dataframe_type == DataframeType.pandas:
            await self._write_dataframe(dataframe, **kwargs)
        elif self.dataframe_type == DataframeType.daft:
            import warnings as _warnings  # noqa: PLC0415

            _warnings.warn(
                "DataframeType.daft is deprecated and will be removed in v4.0; "
                "use DataframeType.pandas instead. Routing to the pandas path.",
                DeprecationWarning,
                stacklevel=3,
            )
            await self._write_dataframe(dataframe, **kwargs)
        else:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                UnsupportedDataframeTypeError,
            )

            raise UnsupportedDataframeTypeError(observed_type=str(self.dataframe_type))

    async def write_batches(
        self,
        dataframe: AsyncGenerator["pd.DataFrame", None]
        | Generator["pd.DataFrame", None, None],
    ) -> None:
        """Write batched DataFrames to the output destination.

        Args:
            dataframe: Async or sync generator yielding DataFrames.

        Raises:
            ValueError: If the writer has been closed or dataframe_type is unsupported.
        """
        if self._is_closed:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                WriterClosedError,
            )

            raise WriterClosedError()

        if self.dataframe_type == DataframeType.pandas:
            await self._write_batched_dataframe(dataframe)
        elif self.dataframe_type == DataframeType.daft:
            import warnings as _warnings  # noqa: PLC0415

            _warnings.warn(
                "DataframeType.daft is deprecated and will be removed in v4.0; "
                "use DataframeType.pandas instead. Routing to the pandas path.",
                DeprecationWarning,
                stacklevel=3,
            )
            await self._write_batched_dataframe(dataframe)
        else:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                UnsupportedDataframeTypeError,
            )

            raise UnsupportedDataframeTypeError(observed_type=str(self.dataframe_type))

    async def _write_batched_dataframe(
        self,
        batched_dataframe: AsyncGenerator["pd.DataFrame", None]
        | Generator["pd.DataFrame", None, None],
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
                        await self._write_dataframe(dataframe)
            else:
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["pd.DataFrame", None, None], batched_dataframe
                )
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self._write_dataframe(dataframe)
        # conformance: ignore[E004] re-raises as typed FormatWriteError; no information is discarded
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatWriteError,
            )

            raise FormatWriteError(cause=e) from e

    async def _write_dataframe(self, dataframe: "pd.DataFrame", **kwargs):
        """Write a pandas DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
            **kwargs: Additional parameters (currently unused for pandas DataFrames).
        """
        try:
            if self.chunk_start is None:
                self.chunk_part = 0
            if len(dataframe) == 0:
                return

            chunk_size_bytes = estimate_dataframe_record_size(dataframe, self.extension)

            for i in range(0, len(dataframe), self.buffer_size):
                chunk = dataframe[i : i + self.buffer_size]

                # Only upload accumulated data if there is any — guards against
                # the first chunk being larger than max_file_size_bytes where
                # no prior _flush_buffer call has written the file yet.
                if (
                    self.current_buffer_size_bytes + chunk_size_bytes
                    > self.max_file_size_bytes
                    and self.current_buffer_size_bytes > 0
                ):
                    output_file_name = os.path.join(
                        self._write_root,
                        path_gen(
                            self.chunk_count,
                            self.chunk_part,
                            extension=self.extension,
                        ),
                    )
                    await self._upload_file(output_file_name)
                    self.chunk_part += 1

                self.current_buffer_size += len(chunk)
                self.current_buffer_size_bytes += chunk_size_bytes * len(chunk)
                await self._flush_buffer(chunk, self.chunk_part)

                del chunk
                gc.collect()

            if self.current_buffer_size_bytes > 0:
                # Finally upload the final file to the object store.
                # _flush_buffer already wrote the file; no existence check needed.
                output_file_name = os.path.join(
                    self._write_root,
                    path_gen(
                        self.chunk_count, self.chunk_part, extension=self.extension
                    ),
                )
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
        # conformance: ignore[E004] records error metrics then re-raises; caller receives the original exception
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={
                    "type": "pandas",
                    "mode": WriteMode.APPEND.value,
                    "error_type": type(e).__name__,
                },
                description="Number of errors while writing to files",
            )
            raise

    @property
    def last_result(self) -> "WriterResult | None":
        """Return the result of the most recent close(), or None if not closed yet.

        Useful when calling close() implicitly via ``async with``: the
        context manager discards close()'s return value, so read it here
        afterwards::

            async with ParquetFileWriter(path=base, typename="t") as w:
                await w.write(df)
            result = w.last_result  # WriterResult
        """
        return self._result

    @property
    def statistics(self) -> TaskStatistics:
        """Get current statistics without closing the writer.

        Returns:
            TaskStatistics: Current statistics (record count, chunk count, partitions).

        Note:
            This returns the current state. For final statistics after all
            writes complete, use close() instead.
        """
        return TaskStatistics(
            total_record_count=self.total_record_count,
            chunk_count=len(self.partitions),
            partitions=self.partitions,
        )

    async def _finalize(self) -> None:
        """Finalize the writer before closing.

        Override this method in subclasses to perform any final flush operations,
        upload remaining files, etc. This is called by close() before writing statistics.
        """

    async def close(self) -> WriterResult:
        """Close the writer, flush buffers, and return statistics + file reference.

        Finalizes all pending writes, writes the statistics sidecar, publishes
        this writer's staged files into ``self.path``, and marks the writer as
        closed. Calling close() multiple times is safe — subsequent calls
        return the cached :class:`WriterResult`.

        Publishing is what keeps one attempt's files out of another's: the
        chunks live in a private staging tree until here, so a cancelled
        attempt's orphaned worker can neither overwrite a retry's file nor slip
        its own files into the retry's ``FileReference`` (FND-317). It only
        ever *adds* — content already under ``self.path`` when the writer
        started is left in place, and a directory ``FileReference`` still walks
        it.

        The returned :class:`WriterResult` carries an ephemeral
        :class:`FileReference` pointing at the writer-owned output directory
        (``self.path``). When that ``FileReference`` is placed on an activity's
        typed Output, the Temporal interceptor's ``persist_file_refs`` uploads
        it transparently with SHA-256 sidecars — callers do not need to call
        ``persist_file_reference`` themselves.

        Returns:
            WriterResult: ``statistics`` (record/chunk counts) and ``files``
                (ephemeral ``FileReference`` to the output directory).

        Raises:
            ValueError: If statistics data is invalid.
            Exception: If there's an error during finalization or writing statistics.

        Example:
            ```python
            async with ParquetFileWriter(path=base, typename="table") as w:
                await w.write(df)
            result = await w.close()
            return MyOutput(statistics=result.statistics, data=result.files)
            ```
        """
        if self._is_closed:
            if self._result is not None:
                return self._result
            # Idempotent fallback: re-derive when called more than once on an
            # already-closed instance with no cached result (defensive — should
            # not happen in normal flow).
            base = self._statistics or self.statistics
            return WriterResult(
                total_record_count=base.total_record_count,
                chunk_count=base.chunk_count,
                partitions=base.partitions,
                typename=base.typename,
                files=self._build_file_reference(),
            )

        try:
            # Allow subclasses to perform final flush/upload operations
            await self._finalize()

            # Use self.typename if available
            typename = getattr(self, "typename", None)

            # Write statistics to file and object store
            statistics_dict = await self._write_statistics(typename)
            if not statistics_dict:
                from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                    MissingStatisticsError,
                )

                raise MissingStatisticsError()

            self._statistics = TaskStatistics(**statistics_dict)
            if typename:
                self._statistics.typename = typename

            # Publish last, once every chunk and the statistics sidecar are on
            # disk: until this returns, the output directory holds nothing this
            # writer produced, and any attempt that dies before reaching here
            # publishes nothing at all.
            self._publish_staged_files()

            self._is_closed = True
            self._result = WriterResult(
                total_record_count=self._statistics.total_record_count,
                chunk_count=self._statistics.chunk_count,
                partitions=self._statistics.partitions,
                typename=self._statistics.typename,
                files=self._build_file_reference(),
            )
            return self._result

        # conformance: ignore[E004] re-raises as typed FormatCloseError; no information is discarded
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatCloseError,
            )

            raise FormatCloseError(cause=e) from e

    def _build_file_reference(self) -> "FileReference | None":
        """Return an ephemeral FileReference for the writer's output directory.

        Only populated when the subclass opts into deferred uploads (e.g.
        ``ParquetFileWriter(defer_uploads=True)``). For the default
        inline-upload path, returns ``None`` so the activity interceptor
        does not double-upload files that are already in the object store.

        Subclasses that defer uploads override this to return
        ``FileReference.from_local(self.path)``.
        """
        return None

    async def _upload_file(self, file_name: str):
        """Upload a staged file to the object store under its published key.

        The key is derived from the *published* path, not the staged one, so
        staging is invisible to the object store: the same chunk reaches the
        same key it always did, whether it is uploaded inline here or moved
        into the output directory at ``close()``.
        """
        retain_local = getattr(self, "retain_local_copy", False)
        await _upload_file(
            self._published_path(file_name), file_name, retain_local_copy=retain_local
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
                output_file_name = os.path.join(
                    self._write_root,
                    path_gen(self.chunk_count, chunk_part, extension=self.extension),
                )
                await self._write_chunk(chunk, output_file_name)

                self.current_buffer_size = 0

                # One buffer chunk on disk is one observable unit of work
                # (ADR-0018). This is the single chunk boundary every writer
                # subclass shares — JsonFileWriter and the non-consolidating
                # ParquetFileWriter path both reach it — so a long
                # `write_batches` stream stays visible to the stall watchdog
                # without any per-record cost.
                current_progress_tracker().mark_progress("writer.flush_buffer")

                # Record chunk metrics
                self.metrics.record_metric(
                    name="chunks_written",
                    value=1,
                    metric_type=MetricType.COUNTER,
                    labels={"type": "output", "mode": WriteMode.APPEND.value},
                    description="Number of chunks written to files",
                )

        # conformance: ignore[E004] records error metrics then re-raises; caller receives the original exception
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={
                    "type": "output",
                    "mode": WriteMode.APPEND.value,
                    "error_type": type(e).__name__,
                },
                description="Number of errors while writing to files",
            )
            raise

    async def _write_statistics(
        self, typename: str | None = None
    ) -> dict[str, Any] | None:
        """Write statistics about the output to a JSON file.

        Internal method called by close() to persist statistics.

        Args:
            typename (str, optional): Type name for organizing statistics.

        Returns:
            Dict containing statistics data.

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

            # Ensure typename is included in the statistics payload (if provided)
            if typename:
                statistics["typename"] = typename

            # Write the statistics to a json file inside a dedicated statistics/ folder
            statistics_dir = os.path.join(self._write_root, "statistics")
            # Inside the guard: creating the directory is itself a write, and
            # on a full filesystem it fails with the same ENOSPC the sidecar
            # write would have — classified identically rather than escaping as
            # a raw OSError that FormatStatisticsWriteError would wrap untyped.
            with disk_full_guard(statistics_dir, operation="writer statistics write"):
                os.makedirs(statistics_dir, exist_ok=True)
            output_file_name = os.path.join(statistics_dir, "statistics.json.ignore")
            # If chunk_start is provided, include it in the statistics filename
            try:
                cs = getattr(self, "chunk_start", None)
                if cs is not None:
                    output_file_name = os.path.join(
                        statistics_dir, f"statistics-chunk-{cs}.json.ignore"
                    )
            except Exception:
                logger.warning(
                    "Failed to access chunk_start for statistics filename, using default",
                    exc_info=True,
                )

            # Write the statistics dictionary to the JSON file. Atomic: the
            # sidecar is the record of how many rows the chunks hold, so a
            # truncated one does not fail loudly — it is read as a smaller,
            # plausible count, and the shortfall is attributed to extraction
            # rather than to the write (FND-318).
            with atomic_write(
                output_file_name, operation="writer statistics write"
            ) as f:
                f.write(orjson.dumps(statistics))

            # Push the file to the object store (key = local path for consistency).
            # ParquetFileWriter with defer_uploads=True overrides _upload_file
            # to a no-op so the statistics sidecar travels via close()'s
            # returned FileReference instead of inline.
            await self._upload_file(output_file_name)

            # The statistics sidecar is the last thing a writer emits, and
            # `close()` runs `_finalize()` (parquet consolidation, remaining
            # uploads) immediately before it. Marking here means the quiet
            # window the watchdog measures starts from the end of the writer's
            # work rather than from its last buffer chunk.
            current_progress_tracker().mark_progress("writer.statistics")

            return statistics
        # conformance: ignore[E004] re-raises as typed FormatStatisticsWriteError; no information is discarded
        except Exception as e:
            from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
                FormatStatisticsWriteError,
            )

            raise FormatStatisticsWriteError(cause=e) from e
