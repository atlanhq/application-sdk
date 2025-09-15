"""IO module for handling data input/output operations.

This module provides base classes and utilities for handling various types of data
input/output operations in the application, including file-based operations and
object store interactions.

The module follows a clean separation of concerns:
- Base Writer class handles common infrastructure (paths, statistics, uploads)
- Format-specific writers handle serialization and format-specific optimizations
- Utilities handle shared functionality across formats

Available Writers:
- Writer: Abstract base class for all writers
- JsonWriter: JSON Lines format writer (from .json)
- ParquetWriter: Parquet format writer (from .parquet)
"""

import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Generator,
    List,
    Optional,
    Union,
)

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class Writer(ABC):
    """Abstract base class for data writers.

    This class provides the common infrastructure for writing data to various formats
    and destinations. It handles path management, statistics tracking, object store
    uploads, and provides a unified interface for all data writers.

    The writer follows a two-phase approach:
    1. write() - Write data to local files (can be called multiple times)
    2. close() - Upload files to object store, generate statistics, and cleanup

    Attributes:
        output_path (str): Local directory where files are written temporarily
        chunk_size (int): Maximum number of records per file
        total_record_count (int): Total records written across all files
        chunk_count (int): Number of files created
        max_file_size_bytes (int): Maximum file size before splitting

    Example:
        >>> writer = JsonWriter(
        ...     output_path="/tmp/data",
        ... )
        >>> await writer.write(dataframe)
        >>> stats = await writer.close()
        >>> print(f"Wrote {stats.total_record_count} records")
    """

    # Class-level constants that subclasses should override
    EXTENSION: str = ""  # File extension (e.g., ".json", ".parquet")

    def __init__(
        self,
        output_path: str,
        chunk_size: int = 100000,
        buffer_size: int = 5000,
        **config: Any,
    ):
        """Initialize the writer with essential configuration.

        Args:
            output_path: Local directory where files will be written temporarily
            chunk_size: Maximum number of records per file before splitting
            buffer_size: Number of records to buffer before flushing to disk
            **config: Additional configuration options:
                - start_marker: Special marker for query extraction file naming
                - end_marker: Special marker for query extraction file naming
                - retain_local_copy: Whether to keep local files after upload

        Raises:
            ValueError: If required parameters are missing or invalid
        """
        if not output_path:
            raise ValueError("output_path is required")

        # Core configuration
        self.output_path = output_path
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size

        # Optional configuration from kwargs
        self.start_marker = config.get("start_marker")
        self.end_marker = config.get("end_marker")
        self.retain_local_copy = config.get("retain_local_copy", False)
        self.typename = config.get("typename")
        self.chunk_start = config.get("chunk_start")

        # Internal state
        self.total_record_count = 0
        self.chunk_count = 0
        self.max_file_size_bytes = int(DAPR_MAX_GRPC_MESSAGE_LENGTH * 0.9)

        # Handle chunk_start like JsonOutput does
        if self.chunk_start:
            self.chunk_count = self.chunk_start + self.chunk_count
        self.chunks_info: List[Dict[str, Any]] = []
        self.created_at = datetime.now(timezone.utc)

        # Buffering state
        self.buffer: List[Any] = []  # Accumulate data here
        self.current_buffer_size = 0  # Number of records in buffer
        self.current_chunk_index = self.chunk_start or 0  # Current chunk file index
        self.current_part_index = 0  # Current part index within chunk

        # Initialize metrics
        self.metrics = get_metrics()

        # Create output directory structure
        self._setup_output_directory()

    def _setup_output_directory(self) -> None:
        """Create the output directory structure."""
        self.full_output_path = self.output_path
        os.makedirs(self.full_output_path, exist_ok=True)

    def _generate_file_path(self, chunk_index: int, part_index: int = 0) -> str:
        """Generate a file path for a given chunk with optional part numbering.

        Args:
            chunk_index: Zero-based index of the chunk
            part_index: Zero-based index of the part within chunk (for splitting large chunks)

        Returns:
            Filename for the chunk (without directory path)

        Note:
            Handles special naming for query extraction using start_marker and end_marker.
            Part numbering allows splitting large chunks: chunk-0-part0.json, chunk-0-part1.json
        """
        if self.start_marker and self.end_marker:
            return f"{self.start_marker}_{self.end_marker}-part{part_index}{self.EXTENSION}"
        else:
            return f"chunk-{chunk_index}-part{part_index}{self.EXTENSION}"

    def _get_full_file_path(self, chunk_index: int, part_index: int = 0) -> str:
        """Get the complete file path including directory.

        Args:
            chunk_index: Zero-based index of the chunk
            part_index: Zero-based index of the part within chunk

        Returns:
            Complete file path for writing
        """
        filename = self._generate_file_path(chunk_index, part_index)
        return os.path.join(self.full_output_path, filename)

    def _should_flush_buffer(self) -> bool:
        """Check if buffer should be flushed based on size limits.

        Returns:
            True if buffer should be flushed, False otherwise
        """
        # Flush if buffer record count exceeds buffer_size limit
        if self.current_buffer_size >= self.buffer_size:
            return True

        return False

    def _should_create_new_chunk(self) -> bool:
        """Check if a new chunk should be created based on total record count.

        Returns:
            True if new chunk should be created, False otherwise
        """
        # Create new chunk if total records in current chunk exceed chunk_size limit
        # Only check when we have at least chunk_size records and it's a multiple of chunk_size
        if (
            self.total_record_count > 0
            and self.total_record_count % self.chunk_size == 0
        ):
            return True

        return False

    def _should_create_new_file(self, file_path: str) -> bool:
        """Check if a new chunk file should be created based on current file size.

        Args:
            file_path: Path to current file to check size

        Returns:
            True if new file should be created, False otherwise
        """
        # Create new file if current file exceeds size limit
        if (
            os.path.exists(file_path)
            and os.path.getsize(file_path) > self.max_file_size_bytes
        ):
            return True

        return False

    def _create_new_file(self, new_chunk: bool = False) -> None:
        """Create a new file by incrementing part or chunk index.

        Args:
            new_chunk: If True, move to next chunk. If False, increment part within same chunk.
        """
        if new_chunk:
            # Move to next chunk and reset part index
            self.current_chunk_index += 1
            self.current_part_index = 0
        else:
            # Stay in same chunk, increment part index
            self.current_part_index += 1

    async def _flush_buffer_to_current_file(self) -> None:
        """Flush current buffer to the current chunk file.

        This method should be implemented by subclasses to handle format-specific
        buffer flushing while maintaining the append-writer pattern.
        """
        if not self.buffer:
            return

        # Get current file path
        file_path = self._get_full_file_path(
            self.current_chunk_index, self.current_part_index
        )

        # Let subclass handle the actual writing
        await self._write_buffer_to_file(self.buffer, file_path)

        # Clear buffer
        self.buffer.clear()
        self.current_buffer_size = 0

        # Check if we need to create a new file part due to size
        if self._should_create_new_file(file_path):
            self._create_new_file(new_chunk=False)

        # Record buffer flush metrics
        self._record_success_metrics("buffer_flush", self.current_buffer_size)

    @abstractmethod
    async def _write_buffer_to_file(self, buffer: List[Any], file_path: str) -> None:
        """Write buffer contents to file (format-specific implementation).

        Args:
            buffer: List of data items to write
            file_path: Complete path to the file
        """
        pass

    async def _finalize_current_file(self) -> None:
        """Finalize the current chunk file and update statistics.

        This method should be called when switching to a new chunk (not for parts).
        """
        # Get all part files for the current chunk
        chunk_files = []
        part_index = 0
        total_chunk_records = 0
        total_chunk_size = 0

        while True:
            file_path = self._get_full_file_path(self.current_chunk_index, part_index)
            if not os.path.exists(file_path):
                break

            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)

            # Count records in this part file by reading lines
            with open(file_path, "r") as f:
                record_count = sum(1 for _ in f)

            chunk_files.append(
                {
                    "file_name": filename,
                    "record_count": record_count,
                    "file_size_bytes": file_size,
                }
            )

            total_chunk_records += record_count
            total_chunk_size += file_size
            part_index += 1

        # Add all parts of this chunk to chunks_info
        if chunk_files:
            # For single part, add directly
            if len(chunk_files) == 1:
                self.chunks_info.append(chunk_files[0])
            else:
                # For multiple parts, add each part
                self.chunks_info.extend(chunk_files)

            # Increment chunk count
            self.chunk_count += 1

    @abstractmethod
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
        **format_options: Any,
    ) -> None:
        """Write data to local files.

        This method can be called multiple times to write different datasets.
        All data is written to local files first, then uploaded when close() is called.

        Args:
            data: DataFrame or generator of DataFrames to write
            **format_options: Format-specific options (varies by writer type)

        Raises:
            ValueError: If data is invalid or empty
            Exception: If writing fails
        """
        pass

    async def close(self, cleanup_local: bool = True) -> ActivityStatistics:
        """Finalize writing by uploading files and generating statistics.

        This method should be called after all write() operations are complete.
        It uploads all local files to the object store, generates statistics,
        and optionally cleans up local files.

        Args:
            cleanup_local: Whether to delete local files after upload

        Returns:
            ActivityStatistics object with processing results

        Raises:
            Exception: If upload or statistics generation fails
        """
        try:
            # Flush any remaining buffer data
            if self.buffer:
                await self._flush_buffer_to_current_file()

            # Finalize current file statistics
            await self._finalize_current_file()

            # Write statistics file first
            stats_data = await self._write_statistics_file()

            # Upload entire directory to object store
            await self._upload_all_files()

            # Cleanup local files if requested
            if cleanup_local and not self.retain_local_copy:
                self._cleanup_local_files()

            # Record final metrics
            self._record_completion_metrics()

            # Return statistics object
            return ActivityStatistics.model_validate(stats_data)

        except Exception as e:
            logger.error(f"Error during close operation: {e}")
            self._record_error_metrics("close_error", str(e))
            raise

    async def _write_statistics_file(self) -> Dict[str, Any]:
        """Write statistics to a local JSON file.

        Returns:
            Dictionary containing the statistics data
        """
        import orjson

        stats_data = self._generate_statistics_data()

        # Write statistics file
        stats_file_path = os.path.join(self.full_output_path, "statistics.json.ignore")
        with open(stats_file_path, "wb") as f:
            f.write(orjson.dumps(stats_data, option=orjson.OPT_INDENT_2))

        logger.info(f"Statistics written to {stats_file_path}")
        return stats_data

    def _generate_statistics_data(self) -> Dict[str, Any]:
        """Generate statistics data dictionary.

        Returns:
            Dictionary containing comprehensive statistics
        """
        return {
            "total_record_count": self.total_record_count,
            "chunk_count": self.chunk_count,
            "chunks_info": self.chunks_info.copy(),
            "format": self.__class__.__name__.lower().replace("writer", ""),
            "created_at": self.created_at.isoformat(),
            "total_file_size_bytes": sum(
                chunk.get("file_size_bytes", 0) for chunk in self.chunks_info
            ),
            "typename": self.typename,
        }

    async def _upload_all_files(self) -> None:
        """Upload all files in the output directory to object store."""
        try:
            destination = get_object_store_prefix(self.full_output_path)
            await ObjectStore.upload_prefix(
                source=self.full_output_path,
                destination=destination,
                retain_local_copy=self.retain_local_copy,
            )
            logger.info(f"Successfully uploaded files to {destination}")

        except Exception as e:
            logger.error(f"Failed to upload files: {e}")
            raise

    def _cleanup_local_files(self) -> None:
        """Remove local files after successful upload."""
        import shutil

        try:
            if os.path.exists(self.full_output_path):
                shutil.rmtree(self.full_output_path)
                logger.info(f"Cleaned up local files in {self.full_output_path}")
        except Exception as e:
            logger.warning(f"Failed to cleanup local files: {e}")

    def _record_success_metrics(
        self, operation: str, record_count: int, description: Optional[str] = None
    ) -> None:
        """Record metrics for successful operations.

        Args:
            operation: Type of operation (e.g., "write", "chunk")
            record_count: Number of records processed
            description: Optional custom description for the metric
        """
        format_name = self.__class__.__name__.lower().replace("writer", "")

        default_description = f"Number of records written in {operation} operation"
        metric_description = description or default_description

        self.metrics.record_metric(
            name="write_records",
            value=record_count,
            metric_type=MetricType.COUNTER,
            labels={"format": format_name, "operation": operation},
            description=metric_description,
        )

    def _record_error_metrics(
        self, error_type: str, error_message: str, description: Optional[str] = None
    ) -> None:
        """Record metrics for error conditions.

        Args:
            error_type: Type of error (e.g., "write_error", "upload_error")
            error_message: Error message for context
            description: Optional custom description for the metric
        """
        format_name = self.__class__.__name__.lower().replace("writer", "")

        default_description = "Number of errors during write operations"
        metric_description = description or default_description

        self.metrics.record_metric(
            name="write_errors",
            value=1,
            metric_type=MetricType.COUNTER,
            labels={
                "format": format_name,
                "error_type": error_type,
                "error": error_message[:100],  # Truncate long error messages
            },
            description=metric_description,
        )

    def _record_completion_metrics(self) -> None:
        """Record metrics for successful completion."""
        format_name = self.__class__.__name__.lower().replace("writer", "")

        self.metrics.record_metric(
            name="chunks_written",
            value=self.chunk_count,
            metric_type=MetricType.COUNTER,
            labels={"format": format_name},
            description="Number of chunks written to files",
        )

        self.metrics.record_metric(
            name="write_operations_completed",
            value=1,
            metric_type=MetricType.COUNTER,
            labels={"format": format_name},
            description="Number of completed write operations",
        )
