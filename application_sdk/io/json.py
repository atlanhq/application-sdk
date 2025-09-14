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
    buffered writing.

    The writer uses orjson for high-performance JSON serialization and supports
    configurable field processing rules for different data cleaning requirements.

    Attributes:
        EXTENSION (str): File extension for JSON files (".json")

    Example:
        >>> writer = JsonWriter(
        ...     output_path="/tmp/data",
        ...     output_suffix="results",
        ...     typename="user_data",
        ...     chunk_start=0,
        ...     chunk_size=50000,
        ...     buffer_size=1000,
        ...     preserve_fields=["id", "name"],
        ...     null_to_empty_dict_fields=["metadata"]
        ... )
        >>> await writer.write(dataframe)
        >>> stats = await writer.close()
    """

    EXTENSION = ".json"

    def __init__(
        self,
        output_path: str,
        output_suffix: str = "",
        chunk_size: int = 100000,
        buffer_size: int = 5000,
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
        typename: Optional[str] = None,
        chunk_start: Optional[int] = None,
        **config: Any,
    ):
        """Initialize JsonWriter with JSON-specific configuration.

        Args:
            output_path: Local directory where files will be written temporarily
            output_suffix: Optional subdirectory under output_path for organization
            chunk_size: Maximum number of records per file before splitting (default: 100000)
            buffer_size: Number of records to buffer before flushing (default: 5000)
            preserve_fields: List of fields to preserve as-is during processing
            null_to_empty_dict_fields: List of fields to convert null values to empty dicts
            typename: Optional type identifier for the data being written
            chunk_start: Optional starting index for chunk numbering
            **config: Additional configuration options including:
                - max_file_size_bytes: Maximum file size before creating new part
                - retain_local_copy: Whether to keep local files after upload
        """
        # Pass typename and chunk_start through config to base class
        config.update(
            {
                "typename": typename,
                "chunk_start": chunk_start,
            }
        )

        super().__init__(
            output_path=output_path,
            output_suffix=output_suffix,
            chunk_size=chunk_size,
            buffer_size=buffer_size,
            **config,
        )

        # Store JsonOutput-compatible parameters for reference
        # (Base class already handles these through config)
        self.typename = typename
        self.chunk_start = chunk_start

        # Set JSON-specific default field processing (like JsonOutput)
        self._default_preserve_fields = [
            "identity_cycle",
            "number_columns_in_part_key",
            "columns_participating_in_part_key",
            "engine",
            "is_insertable_into",
            "is_typed",
        ]
        self._default_null_to_empty_dict_fields = [
            "attributes",
            "customAttributes",
        ]

        # Store JSON-specific parameters for use in write method
        self._preserve_fields = preserve_fields or self._default_preserve_fields
        self._null_to_empty_dict_fields = (
            null_to_empty_dict_fields or self._default_null_to_empty_dict_fields
        )

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
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
        **format_options: Any,
    ) -> None:
        """Write data to JSON Lines format files with buffered writing.

        This method handles both pandas and daft DataFrames internally, processing
        them with the same buffer management approach as JsonOutput.

        Args:
            data: DataFrame or generator of DataFrames to write
            preserve_fields: List of field names to preserve even if they contain null values
            null_to_empty_dict_fields: List of field names to convert from null to empty dict
            **format_options: Additional format-specific options (reserved for future use)

        Raises:
            ValueError: If data is invalid or unsupported type
            Exception: If writing operation fails
        """
        try:
            # Process all DataFrames with unified approach
            async for dataframe in normalize_to_async_iterator(data):
                await self._process_dataframe(dataframe)

        except Exception as e:
            logger.error(f"Error writing data to JSON: {e}")
            self._record_error_metrics("write_error", str(e))
            raise

    async def _process_dataframe(
        self, dataframe: Union["pd.DataFrame", "daft.DataFrame"]
    ) -> None:
        """Process DataFrame internally - handles pandas vs daft automatically."""
        if is_empty_dataframe(dataframe):
            return

        if is_pandas_dataframe(dataframe):
            await self._process_pandas_dataframe(dataframe)
        elif is_daft_dataframe(dataframe):
            await self._process_daft_dataframe(dataframe)
        else:
            raise ValueError(f"Unsupported DataFrame type: {type(dataframe)}")

    async def _process_pandas_dataframe(self, dataframe: "pd.DataFrame") -> None:
        """Process pandas DataFrame - simplified approach like JsonOutput."""
        try:
            # Use pandas to_json like JsonOutput does
            json_lines_str = dataframe.to_json(orient="records", lines=True)

            if json_lines_str:
                for line in json_lines_str.strip().split("\n"):
                    if line.strip():
                        # Process each record
                        row_data = orjson.loads(line)
                        row_data = convert_datetime_to_epoch(row_data)
                        row_data = process_null_fields(
                            row_data,
                            self._preserve_fields,
                            self._null_to_empty_dict_fields,
                        )

                        # Add to buffer
                        serialized = orjson.dumps(
                            row_data, option=orjson.OPT_APPEND_NEWLINE
                        )
                        self.buffer.append(serialized)
                        self.current_buffer_size += 1
                        self.total_record_count += 1

                        # Check if we need to create a new chunk
                        if self._should_create_new_chunk():
                            # Flush current buffer first
                            if self.buffer:
                                await self._flush_buffer_to_current_file()
                            # Finalize current file statistics
                            await self._finalize_current_file()
                            # Move to next chunk
                            self._create_new_file(new_chunk=True)

                        # Flush buffer when buffer_size reached (like JsonOutput)
                        elif self._should_flush_buffer():
                            await self._flush_buffer_to_current_file()

            # Record success metrics
            self._record_success_metrics("pandas_write", len(dataframe))

        except Exception as e:
            logger.error(f"Error processing pandas DataFrame: {e}")
            raise

    async def _process_daft_dataframe(
        self,
        dataframe: "daft.DataFrame",
    ) -> None:
        """Process daft DataFrame - row-by-row like JsonOutput."""
        try:
            record_count = 0

            # Process row by row for memory efficiency (like JsonOutput)
            for row in dataframe.iter_rows():
                # Process row
                row_data = convert_datetime_to_epoch(row)
                row_data = process_null_fields(
                    row_data, self._preserve_fields, self._null_to_empty_dict_fields
                )

                # Serialize and add to buffer
                serialized = orjson.dumps(row_data, option=orjson.OPT_APPEND_NEWLINE)
                self.buffer.append(serialized)
                self.current_buffer_size += 1
                self.total_record_count += 1
                record_count += 1

                # Check if we need to create a new chunk
                if self._should_create_new_chunk():
                    # Flush current buffer first
                    if self.buffer:
                        await self._flush_buffer_to_current_file()
                    # Finalize current file statistics
                    await self._finalize_current_file()
                    # Move to next chunk
                    self._create_new_file(new_chunk=True)

                # Flush buffer when buffer_size reached (like JsonOutput)
                elif self._should_flush_buffer():
                    await self._flush_buffer_to_current_file()

            # Record success metrics
            self._record_success_metrics("daft_write", record_count)

        except Exception as e:
            logger.error(f"Error processing daft DataFrame: {e}")
            raise

    async def _write_buffer_to_file(self, buffer: List[bytes], file_path: str) -> None:
        """Write buffer contents to JSON file (append mode) - like JsonOutput."""
        mode = "ab" if os.path.exists(file_path) else "wb"

        with open(file_path, mode) as f:
            f.writelines(buffer)

        logger.info(f"Wrote {len(buffer)} JSON records to {file_path}")
