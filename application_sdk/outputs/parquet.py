import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.outputs.objectstore import ObjectStoreOutput

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class ChunkedObjectStoreWriterInterface(ABC):
    """Abstract base class for chunked object store writers.

    This class provides a common interface for writing data in chunks to files
    and uploading them to an object store.

    Attributes:
        local_file_prefix (str): Prefix for local file paths.
        upload_file_prefix (str): Prefix for files when uploading to object store.
        chunk_size (int): Maximum number of records per chunk.
        buffer_size (int): Size of the buffer in bytes.
        current_file: Current file being written to.
        current_file_name (str): Name of the current file.
        current_file_number (int): Current chunk number.
        current_record_count (int): Number of records in current chunk.
        total_record_count (int): Total number of records processed.
        buffer (List[str]): Buffer holding records before writing.
        current_buffer_size (int): Current size of the buffer.
    """

    def __init__(
        self,
        local_file_prefix: str,
        upload_file_prefix: str,
        chunk_size: int = 30000,
        buffer_size: int = 1024 * 1024 * 10,
        start_file_number: int = 0,
    ):  # 10MB buffer by default
        """Initialize the chunked object store writer.

        Args:
            local_file_prefix (str): Prefix for local file paths.
            upload_file_prefix (str): Prefix for files when uploading to object store.
            chunk_size (int, optional): Maximum records per chunk. Defaults to 30000.
            buffer_size (int, optional): Buffer size in bytes. Defaults to 10MB.
            start_file_number (int, optional): Starting chunk number. Defaults to 0.
        """
        self.local_file_prefix = local_file_prefix
        self.upload_file_prefix = upload_file_prefix
        self.chunk_size = chunk_size
        self.lock = asyncio.Lock()
        self.current_file = None
        self.current_file_name = None
        self.current_file_number = start_file_number
        self.current_record_count = 0
        self.total_record_count = 0

        self.buffer: List[str] = []
        self.buffer_size = buffer_size
        self.current_buffer_size = 0

        os.makedirs(self.local_file_prefix, exist_ok=True)

    @abstractmethod
    async def write(self, data: Dict[str, Any]) -> None:
        """Write a single record to the output.

        Args:
            data (Dict[str, Any]): Record to write.
        """
        raise NotImplementedError

    async def write_list(self, data: List[Dict[str, Any]]) -> None:
        """Write multiple records to the output.

        Args:
            data (List[Dict[str, Any]]): List of records to write.
        """
        for record in data:
            await self.write(record)

    @abstractmethod
    async def close(self) -> int:
        """Close the current file and clean up resources.

        Returns:
            int: Total number of records written.
        """
        raise NotImplementedError

    async def close_current_file(self):
        """Close the current file and upload it to object store.

        This method closes the current file if one is open, uploads it to
        the object store, and optionally removes the local copy.
        """
        if not self.current_file:
            return

        await self.current_file.close()
        await self.upload_file(self.current_file_name)
        # os.unlink(self.current_file_name)
        activity.logger.info(
            f"Uploaded file: {self.current_file_name} and removed local copy"
        )

    async def upload_file(self, local_file_path: str) -> None:
        """Upload a file to the object store.

        Args:
            local_file_path (str): Path to the local file to upload.
        """
        activity.logger.info(
            f"Uploading file: {local_file_path} to {self.upload_file_prefix}"
        )
        await ObjectStoreOutput.push_file_to_object_store(
            self.upload_file_prefix, local_file_path
        )

    async def __aenter__(self):
        """Enter the async context.

        Returns:
            ChunkedObjectStoreWriterInterface: The writer instance.
        """
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        """Exit the async context.

        Args:
            exc_type: Type of the exception that occurred, if any.
            exc_value: The exception instance that occurred, if any.
            traceback: The traceback of the exception that occurred, if any.

        Returns:
            bool: False to propagate exceptions, if any.
        """
        await self.close()
        return False


class ParquetChunkedObjectStoreWriter(ChunkedObjectStoreWriterInterface):
    """Chunked object store writer for Parquet files.

    This class handles writing data to Parquet files with support for schema
    evolution and automatic uploading to object store.

    Attributes:
        schema (pq.ParquetSchema): Schema for the Parquet files.
        parquet_writer_options (Dict[str, Any]): Options for Parquet writer.
    """

    def __init__(
        self,
        local_file_prefix: str,
        upload_file_prefix: str,
        chunk_size: int = 100000,
        schema: pq.ParquetSchema = None,
        parquet_writer_options: Dict[str, Any] = {},
    ):
        """Initialize the Parquet writer.

        Args:
            local_file_prefix (str): Prefix for local file paths.
            upload_file_prefix (str): Prefix for files when uploading to object store.
            chunk_size (int, optional): Maximum records per chunk. Defaults to 100000.
            schema (pq.ParquetSchema, optional): Initial schema. Defaults to None.
            parquet_writer_options (Dict[str, Any], optional): Writer options.
                Defaults to {}.
        """
        super().__init__(local_file_prefix, upload_file_prefix, chunk_size)
        self.schema = schema
        self.parquet_writer_options = parquet_writer_options

    async def update_schema(self, new_schema: pq.ParquetSchema):
        """Update the schema by merging with a new schema.

        This method handles schema evolution by merging the existing schema
        with new fields from the provided schema.

        Args:
            new_schema (pq.ParquetSchema): New schema to merge with existing one.
        """
        if self.schema is None:
            self.schema = new_schema
        else:
            # Merge the existing schema with the new one
            merged_fields = list(self.schema)
            for field in new_schema:
                if field.name not in self.schema.names:
                    merged_fields.append(field)
            self.schema = pa.schema(merged_fields)

    async def write(self, data: Dict[str, Any]) -> None:
        """Write a single record to a Parquet file.

        This method handles schema evolution and ensures the data conforms
        to the current schema before writing.

        Args:
            data (Dict[str, Any]): Record to write.
        """
        async with self.lock:
            if (
                self.current_file is None
                or self.current_record_count >= self.chunk_size
            ):
                await self._create_new_file()

            table = pa.Table.from_pydict(data)
            new_schema = table.schema

            await self.update_schema(new_schema)
            # Ensure the table conforms to the current schema
            table = table.cast(self.schema)
            self.current_file.write_table(table)

            self.current_record_count += 1
            self.total_record_count += 1

    async def close(self) -> None:
        """Close the current file and write metadata.

        This method closes the current file, writes metadata about the chunks,
        and uploads both to the object store.
        """
        await self.close_current_file()

        # Write number of chunks
        with open(f"{self.local_file_prefix}-metadata.json.ignore", mode="w") as f:
            f.write(
                orjson.dumps(
                    {
                        "total_record_count": self.total_record_count,
                        "chunk_count": self.current_file_number,
                    },
                    option=orjson.OPT_APPEND_NEWLINE,
                ).decode("utf-8")
            )
        await self.upload_file(f"{self.local_file_prefix}-metadata.json.ignore")

    async def _create_new_file(self):
        """Create a new Parquet file for writing.

        This method closes the current file if one exists, creates a new file
        with the current schema, and initializes it for writing.
        """
        await self.close_current_file()

        self.current_file_number += 1
        self.current_file_name = (
            f"{self.local_file_prefix}_{self.current_file_number}.parquet"
        )
        self.current_file = pq.ParquetWriter(
            self.current_file_name, self.schema, **self.parquet_writer_options
        )

        activity.logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0
