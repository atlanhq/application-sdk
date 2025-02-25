import os
import types
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

import pandas as pd
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.config import get_settings
from application_sdk.outputs import Output
from application_sdk.outputs.objectstore import ObjectStoreOutput

activity.logger = get_logger(__name__)


class ParquetOutput(Output):
    """Output handler for writing data to Parquet files.

    This class handles writing DataFrames to Parquet files with support for chunking
    and automatic uploading to object store.

    Attributes:
        output_path (str): Base path where Parquet files will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        output_suffix (str): Suffix for output files.
        typename (Optional[str]): Type name of the entity e.g database, schema, table.
        mode (str): Write mode for parquet files ("append" or "overwrite").
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks created.
    """

    def __init__(
        self,
        local_file_prefix: str,
        upload_file_prefix: str,
        chunk_size: Optional[int] = None,
        buffer_size: int = 1024 * 1024 * 10,
        start_file_number: int = 0,
    ):  # 10MB buffer by default
        """Initialize the chunked object store writer.

        Args:
            local_file_prefix (str): Prefix for local file paths.
            upload_file_prefix (str): Prefix for files when uploading to object store.
            chunk_size (Optional[int], optional): Maximum records per chunk. If None, uses config value.
            buffer_size (int, optional): Buffer size in bytes. Defaults to 10MB.
            start_file_number (int, optional): Starting chunk number. Defaults to 0.
        """
        self.local_file_prefix = local_file_prefix
        self.upload_file_prefix = upload_file_prefix
        settings = get_settings()
        self.chunk_size = chunk_size or settings.chunk_size
        self.lock = asyncio.Lock()
        self.current_file = None
        self.current_file_name = None
        self.current_file_number = start_file_number
        self.current_record_count = 0
        self.total_record_count = 0

        self.buffer: List[str] = []
        self.buffer_size = buffer_size
        self.current_buffer_size = 0

        # Create output directory
        full_path = f"{output_path}{output_suffix}"
        if typename:
            full_path = f"{full_path}/{typename}"
        os.makedirs(full_path, exist_ok=True)

    async def write_dataframe(self, dataframe: pd.DataFrame):
        """Write a pandas DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
        """
        try:
            if len(dataframe) == 0:
                return

            # Update counters
            self.chunk_count += 1
            self.total_record_count += len(dataframe)

            # Generate output file path
            file_path = f"{self.output_path}{self.output_suffix}"
            if self.typename:
                file_path = f"{file_path}/{self.typename}"
            file_path = f"{file_path}_{self.chunk_count}.parquet"

            # Write the dataframe to parquet using pandas native method
            dataframe.to_parquet(
                file_path,
                index=False,
                compression="snappy",  # Using snappy compression by default
            )

            # Upload the file to object store
            await self.upload_file(file_path)
        except Exception as e:
            activity.logger.error(
                f"Error writing pandas dataframe to parquet: {str(e)}"
            )
            raise

    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
        """
        try:
            if dataframe.count_rows() == 0:
                return

            # Update counters
            self.chunk_count += 1
            self.total_record_count += dataframe.count_rows()

            # Generate output file path
            file_path = f"{self.output_path}{self.output_suffix}"
            if self.typename:
                file_path = f"{file_path}/{self.typename}"
            file_path = f"{file_path}_{self.chunk_count}.parquet"

            # Write the dataframe to parquet using daft
            dataframe.write_parquet(
                file_path,
                write_mode="overwrite" if self.mode == "overwrite" else "append",
            )

            # Upload the file to object store
            await self.upload_file(file_path)
        except Exception as e:
            activity.logger.error(f"Error writing daft dataframe to parquet: {str(e)}")
            raise

    async def upload_file(self, local_file_path: str) -> None:
        """Upload a file to the object store.

        Args:
            local_file_path (str): Path to the local file to upload.
        """
        activity.logger.info(
            f"Uploading file: {local_file_path} to {self.output_prefix}"
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

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[types.TracebackType],
    ) -> bool:
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
        chunk_size: Optional[int] = None,
        schema: pq.ParquetSchema = None,
        parquet_writer_options: Dict[str, Any] = {},
    ):
        """Initialize the Parquet writer.

        Args:
            local_file_prefix (str): Prefix for local file paths.
            upload_file_prefix (str): Prefix for files when uploading to object store.
            chunk_size (Optional[int], optional): Maximum records per chunk. If None, uses config value.
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
