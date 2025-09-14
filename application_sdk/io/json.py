"""JSON writer implementation for data output operations.

This module provides the JsonWriter class for writing DataFrames to JSON Lines format.
It supports both pandas and daft DataFrames with format-specific optimizations and
handles JSON-specific processing like datetime conversion and null field handling.
"""

import os
from typing import TYPE_CHECKING, Any, AsyncGenerator, Generator, List, Optional, Union

import orjson

from application_sdk.io import Writer
from application_sdk.io._utils import (
    convert_datetime_to_epoch,
    estimate_json_size,
    is_daft_dataframe,
    is_empty_dataframe,
    is_pandas_dataframe,
    normalize_to_async_iterator,
    process_null_fields,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class JsonWriter(Writer):
    """Writer for JSON Lines format output.

    This writer handles conversion of DataFrames to JSON Lines format with support
    for both pandas and daft DataFrames. It provides JSON-specific processing
    including datetime conversion, null field handling, and memory-efficient
    row-by-row processing for large datasets.

    The writer uses orjson for high-performance JSON serialization and supports
    configurable field processing rules for different data cleaning requirements.

    Attributes:
        EXTENSION (str): File extension for JSON files (".json")

    Example:
        >>> writer = JsonWriter(
        ...     output_path="/tmp/data",
        ...     output_prefix="s3://bucket/results"
        ... )
        >>> await writer.write(
        ...     dataframe,
        ...     preserve_fields=["id", "name"],
        ...     null_to_empty_dict_fields=["metadata"]
        ... )
        >>> stats = await writer.close()
    """

    EXTENSION = ".json"

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
        buffered: bool = True,
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
        **format_options: Any,
    ) -> None:
        """Write data to JSON Lines format files with intelligent buffering.

        This method implements an append-writer pattern where multiple write() calls
        accumulate data into the same file until size thresholds are exceeded.

        Args:
            data: DataFrame or generator of DataFrames to write
            buffered: Whether to use buffered writing (True = append-writer, False = immediate)
            preserve_fields: List of field names to preserve even if they contain null values
            null_to_empty_dict_fields: List of field names to convert from null to empty dict
            **format_options: Additional format-specific options (reserved for future use)

        Raises:
            ValueError: If data is invalid or unsupported type
            Exception: If writing operation fails
        """
        try:
            if buffered:
                # Phase 2: Buffered append-writer approach
                await self._write_buffered(
                    data, preserve_fields, null_to_empty_dict_fields
                )
            else:
                # Phase 1: Immediate writing approach
                await self._write_immediate(
                    data, preserve_fields, null_to_empty_dict_fields
                )

        except Exception as e:
            logger.error(f"Error writing data to JSON: {e}")
            self._record_error_metrics("write_error", str(e))
            raise

    async def _write_buffered(
        self,
        data: Union[
            "pd.DataFrame",
            "daft.DataFrame",
            Generator["pd.DataFrame", None, None],
            AsyncGenerator["pd.DataFrame", None],
            Generator["daft.DataFrame", None, None],
            AsyncGenerator["daft.DataFrame", None],
        ],
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> None:
        """Write data using buffered append-writer approach (Phase 2)."""
        # Normalize input to async iterator
        async for dataframe in normalize_to_async_iterator(data):
            if is_pandas_dataframe(dataframe):
                await self._add_pandas_to_buffer(
                    dataframe, preserve_fields, null_to_empty_dict_fields
                )
            elif is_daft_dataframe(dataframe):
                await self._add_daft_to_buffer(
                    dataframe, preserve_fields, null_to_empty_dict_fields
                )
            else:
                raise ValueError(f"Unsupported DataFrame type: {type(dataframe)}")

    async def _write_immediate(
        self,
        data: Union[
            "pd.DataFrame",
            "daft.DataFrame",
            Generator["pd.DataFrame", None, None],
            AsyncGenerator["pd.DataFrame", None],
            Generator["daft.DataFrame", None, None],
            AsyncGenerator["daft.DataFrame", None],
        ],
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> None:
        """Write data immediately without buffering (Phase 1 behavior)."""
        # Normalize input to async iterator
        async for dataframe in normalize_to_async_iterator(data):
            if is_pandas_dataframe(dataframe):
                await self._write_pandas_dataframe_immediate(
                    dataframe, preserve_fields, null_to_empty_dict_fields
                )
            elif is_daft_dataframe(dataframe):
                await self._write_daft_dataframe_immediate(
                    dataframe, preserve_fields, null_to_empty_dict_fields
                )
            else:
                raise ValueError(f"Unsupported DataFrame type: {type(dataframe)}")

    async def _add_pandas_to_buffer(
        self,
        dataframe: "pd.DataFrame",
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> None:
        """Add pandas DataFrame to buffer with intelligent flushing (Phase 2)."""
        if is_empty_dataframe(dataframe):
            return

        try:
            # Process DataFrame in chunks to manage memory
            for i in range(0, len(dataframe), self.chunk_size):
                chunk = dataframe[i : i + self.chunk_size]

                # Estimate size BEFORE expensive serialization
                estimated_size = estimate_json_size(chunk)

                # Check if we need to create a new file BEFORE serialization
                if self._should_create_new_file(estimated_size):
                    # Flush current buffer and create new part
                    if self.buffer:
                        await self._flush_buffer_to_current_file()
                    # Create new part in same chunk (not new chunk)
                    self._create_new_file(new_chunk=False)

                # Now do the expensive serialization
                json_records = await self._pandas_chunk_to_json_records(
                    chunk, preserve_fields, null_to_empty_dict_fields
                )

                # Use actual size for buffer tracking (more accurate)
                actual_size = sum(len(record) for record in json_records)

                # Add records to buffer
                self.buffer.extend(json_records)
                self.current_buffer_size += len(json_records)
                self.current_buffer_size_bytes += actual_size
                self.total_record_count += len(chunk)

                # Check if buffer should be flushed
                if self._should_flush_buffer():
                    await self._flush_buffer_to_current_file()

                # Record success metrics
                self._record_success_metrics("pandas_buffered", len(chunk))

        except Exception as e:
            logger.error(f"Error adding pandas DataFrame to buffer: {e}")
            raise

    async def _add_daft_to_buffer(
        self,
        dataframe: "daft.DataFrame",
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> None:
        """Add daft DataFrame to buffer with intelligent flushing (Phase 2)."""
        if is_empty_dataframe(dataframe):
            return

        try:
            # Estimate total size BEFORE processing rows
            total_estimated_size = estimate_json_size(dataframe)

            # Check if we need a new file for the entire DataFrame
            if self._should_create_new_file(total_estimated_size):
                if self.buffer:
                    await self._flush_buffer_to_current_file()
                # Create new part in same chunk
                self._create_new_file(new_chunk=False)

            # Process daft DataFrame row by row for memory efficiency
            json_records = []
            record_count = 0

            for row in dataframe.iter_rows():
                # Process row
                processed_row = convert_datetime_to_epoch(row)
                if preserve_fields or null_to_empty_dict_fields:
                    processed_row = process_null_fields(
                        processed_row, preserve_fields, null_to_empty_dict_fields
                    )

                # Serialize row
                serialized_row = orjson.dumps(
                    processed_row, option=orjson.OPT_APPEND_NEWLINE
                )
                json_records.append(serialized_row)
                record_count += 1

                # Flush buffer periodically to manage memory
                if len(json_records) >= self.chunk_size:
                    actual_size = sum(len(record) for record in json_records)

                    # Add to buffer (file decision already made above)
                    self.buffer.extend(json_records)
                    self.current_buffer_size += len(json_records)
                    self.current_buffer_size_bytes += actual_size
                    self.total_record_count += len(json_records)

                    # Clear temporary records
                    json_records = []

                    # Check if buffer should be flushed
                    if self._should_flush_buffer():
                        await self._flush_buffer_to_current_file()

            # Handle remaining records
            if json_records:
                actual_size = sum(len(record) for record in json_records)

                self.buffer.extend(json_records)
                self.current_buffer_size += len(json_records)
                self.current_buffer_size_bytes += actual_size
                self.total_record_count += len(json_records)

                if self._should_flush_buffer():
                    await self._flush_buffer_to_current_file()

            # Record success metrics
            self._record_success_metrics("daft_buffered", record_count)

        except Exception as e:
            logger.error(f"Error adding daft DataFrame to buffer: {e}")
            raise

    async def _pandas_chunk_to_json_records(
        self,
        chunk: "pd.DataFrame",
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> List[bytes]:
        """Convert pandas DataFrame chunk to JSON records."""
        # Convert to JSON lines
        json_lines_str = chunk.to_json(orient="records", lines=True)

        # Process each line
        processed_records = []
        if json_lines_str:
            for line in json_lines_str.strip().split("\n"):
                if line.strip():
                    # Parse, process, and re-serialize
                    row_data = orjson.loads(line)
                    row_data = convert_datetime_to_epoch(row_data)

                    if preserve_fields or null_to_empty_dict_fields:
                        row_data = process_null_fields(
                            row_data, preserve_fields, null_to_empty_dict_fields
                        )

                    serialized = orjson.dumps(
                        row_data, option=orjson.OPT_APPEND_NEWLINE
                    )
                    processed_records.append(serialized)

        return processed_records

    async def _write_buffer_to_file(self, buffer: List[Any], file_path: str) -> None:
        """Write buffer contents to JSON file (append mode)."""
        mode = "ab" if os.path.exists(file_path) else "wb"

        with open(file_path, mode) as f:
            f.writelines(buffer)

        logger.info(f"Wrote {len(buffer)} JSON records to {file_path}")

    async def _write_pandas_dataframe_immediate(
        self,
        dataframe: "pd.DataFrame",
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> None:
        """Write a pandas DataFrame immediately (Phase 1 behavior)."""
        if is_empty_dataframe(dataframe):
            return

        try:
            # Estimate size before processing (for metrics/logging)
            # estimated_size = estimate_json_size(dataframe)

            # Get file path for this chunk
            file_path = self._get_full_file_path(
                self.current_chunk_index, self.current_part_index
            )

            # Convert DataFrame to JSON records
            json_records = await self._pandas_chunk_to_json_records(
                dataframe, preserve_fields, null_to_empty_dict_fields
            )

            # Write to file
            with open(file_path, "wb") as f:
                f.writelines(json_records)

            # Update statistics
            record_count = len(dataframe)
            file_size = os.path.getsize(file_path)
            self._update_statistics_immediate(record_count, file_path, file_size)

            # Record success metrics
            self._record_success_metrics("pandas_immediate", record_count)

            logger.info(f"Wrote {record_count} pandas records to {file_path}")

        except Exception as e:
            logger.error(f"Error writing pandas DataFrame immediately: {e}")
            raise

    async def _write_daft_dataframe_immediate(
        self,
        dataframe: "daft.DataFrame",
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> None:
        """Write a daft DataFrame immediately (Phase 1 behavior)."""
        if is_empty_dataframe(dataframe):
            return

        try:
            # Estimate size before processing (for metrics/logging)
            # estimated_size = estimate_json_size(dataframe)

            # Get file path for this chunk
            file_path = self._get_full_file_path(
                self.current_chunk_index, self.current_part_index
            )

            # Process rows one by one for memory efficiency
            processed_lines = []
            record_count = 0

            for row in dataframe.iter_rows():
                # Apply datetime conversion
                row = convert_datetime_to_epoch(row)

                # Apply null field processing
                if preserve_fields or null_to_empty_dict_fields:
                    row = process_null_fields(
                        row, preserve_fields, null_to_empty_dict_fields
                    )

                # Serialize row with orjson
                serialized_row = orjson.dumps(row, option=orjson.OPT_APPEND_NEWLINE)
                processed_lines.append(serialized_row)
                record_count += 1

            # Write all processed lines to file
            with open(file_path, "wb") as f:
                f.writelines(processed_lines)

            # Update statistics
            file_size = os.path.getsize(file_path)
            self._update_statistics_immediate(record_count, file_path, file_size)

            # Record success metrics
            self._record_success_metrics("daft_immediate", record_count)

            logger.info(f"Wrote {record_count} daft records to {file_path}")

        except Exception as e:
            logger.error(f"Error writing daft DataFrame immediately: {e}")
            raise

    def _update_statistics_immediate(
        self, record_count: int, file_path: str, file_size: int
    ) -> None:
        """Update statistics for immediate writing (Phase 1 behavior)."""
        # Update totals
        self.total_record_count += record_count

        # Add chunk info
        filename = os.path.basename(file_path)
        chunk_info = {
            "file_name": filename,
            "record_count": record_count,
            "file_size_bytes": file_size,
        }
        self.chunks_info.append(chunk_info)

        # Increment chunk count and move to next chunk
        self.chunk_count += 1
        self._create_new_file(new_chunk=True)
