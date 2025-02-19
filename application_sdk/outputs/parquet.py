import asyncio
import os
from typing import Any, Dict, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.outputs import Output
from application_sdk.outputs.objectstore import ObjectStoreOutput

activity.logger = get_logger(__name__)

# Type alias for PyArrow Schema to help with type hints
PyArrowSchema = pa.Schema


class ParquetOutput(Output):
    """Output handler for writing data to Parquet files.

    This class handles writing DataFrames to Parquet files with support for chunking
    and automatic uploading to object store.

    Attributes:
        output_path (str): Path where Parquet files will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        chunk_size (int): Maximum number of records per chunk.
        buffer_size (int): Size of the buffer in bytes.
        current_file: Current file being written to.
        current_file_name (str): Name of the current file.
        current_file_number (int): Current chunk number.
        current_record_count (int): Number of records in current chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks created.
        schema (PyArrowSchema): Schema for the Parquet files.
        parquet_writer_options (Dict[str, Any]): Options for Parquet writer.
    """

    output_path: str
    output_prefix: str
    output_suffix: str
    typename: Optional[str]
    state: Optional[ActivitiesState]
    chunk_size: int
    buffer_size: int
    total_record_count: int
    chunk_count: int
    schema: Optional[PyArrowSchema]
    parquet_writer_options: Dict[str, Any]
    current_file: Optional[pq.ParquetWriter]
    current_file_name: Optional[str]
    current_record_count: int
    lock: asyncio.Lock

    def __init__(
        self,
        output_suffix: str,
        output_path: str,
        output_prefix: str,
        typename: Optional[str] = None,
        state: Optional[ActivitiesState] = None,
        chunk_size: int = 100000,
        buffer_size: int = 1024 * 1024 * 10,  # 10MB buffer by default
        total_record_count: int = 0,
        chunk_count: int = 0,
        schema: Optional[PyArrowSchema] = None,
        parquet_writer_options: Optional[Dict[str, Any]] = None,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the Parquet output handler.

        Args:
            output_path (str): Path where Parquet files will be written.
            output_suffix (str): Suffix for output files.
            output_prefix (str): Prefix for files where the files will be written and uploaded.
            typename (Optional[str], optional): Type name of the entity e.g database, schema, table.
            state (Optional[ActivitiesState], optional): State object for the activity.
            chunk_size (int, optional): Maximum records per chunk. Defaults to 100000.
            buffer_size (int, optional): Buffer size in bytes. Defaults to 10MB.
            total_record_count (int, optional): Initial total record count. Defaults to 0.
            chunk_count (int, optional): Initial chunk count. Defaults to 0.
            schema (Optional[PyArrowSchema], optional): Initial schema. Defaults to None.
            parquet_writer_options (Optional[Dict[str, Any]], optional): Writer options. Defaults to None.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.state = state
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.schema = schema
        self.parquet_writer_options = parquet_writer_options or {}
        self.lock = asyncio.Lock()
        self.current_file = None
        self.current_file_name = None
        self.current_record_count = 0

        os.makedirs(self.output_path, exist_ok=True)

    @classmethod
    def re_init(
        cls,
        output_path: str,
        output_suffix: str,
        output_prefix: str,
        typename: Optional[str] = None,
        chunk_count: int = 0,
        total_record_count: int = 0,
        chunk_size: Optional[int] = None,
        buffer_size: Optional[int] = None,
        schema: Optional[PyArrowSchema] = None,
        parquet_writer_options: Optional[Dict[str, Any]] = None,
        **kwargs: Dict[str, Any],
    ):
        """Re-initialize the output class with given keyword arguments.

        Args:
            output_path (str): Path where Parquet files will be written.
            output_suffix (str): Suffix for output files.
            output_prefix (str): Prefix for files where the files will be written and uploaded.
            typename (str, optional): Type name of the entity e.g database, schema, table.
                Defaults to None.
            chunk_count (int, optional): Initial chunk count. Defaults to 0.
            total_record_count (int, optional): Initial total record count. Defaults to 0.
            chunk_size (Optional[int], optional): Maximum records per chunk. Defaults to None.
            buffer_size (Optional[int], optional): Buffer size in bytes. Defaults to None.
            schema (Optional[PyArrowSchema], optional): Initial schema. Defaults to None.
            parquet_writer_options (Optional[Dict[str, Any]], optional): Writer options. Defaults to None.
            kwargs (Dict[str, Any]): Additional keyword arguments.
        """
        output_path = f"{output_path}{output_suffix}"
        if typename:
            output_path = f"{output_path}/{typename}"
        os.makedirs(f"{output_path}", exist_ok=True)

        init_kwargs = {
            "output_suffix": output_suffix,
            "output_path": output_path,
            "output_prefix": output_prefix,
            "typename": typename,
            "chunk_count": chunk_count,
            "total_record_count": total_record_count,
        }
        if chunk_size is not None:
            init_kwargs["chunk_size"] = chunk_size
        if buffer_size is not None:
            init_kwargs["buffer_size"] = buffer_size
        if schema is not None:
            init_kwargs["schema"] = schema
        if parquet_writer_options is not None:
            init_kwargs["parquet_writer_options"] = parquet_writer_options

        return cls(**init_kwargs)

    async def update_schema(self, new_schema: PyArrowSchema):
        """Update the schema by merging with a new schema.

        This method handles schema evolution by merging the existing schema
        with new fields from the provided schema.

        Args:
            new_schema (PyArrowSchema): New schema to merge with existing one.
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

    async def write_dataframe(self, dataframe: pd.DataFrame):
        """Write a pandas DataFrame to Parquet files.

        This method handles schema evolution and ensures the data conforms
        to the current schema before writing.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
        """
        if len(dataframe) == 0:
            return

        try:
            async with self.lock:
                if self.current_file is None:
                    await self._create_new_file()

                table = pa.Table.from_pandas(dataframe)
                new_schema = table.schema

                await self.update_schema(new_schema)
                # Ensure the table conforms to the current schema
                table = table.cast(self.schema)
                self.current_file.write_table(table)

                self.current_record_count += len(dataframe)
                self.total_record_count += len(dataframe)

                if self.current_record_count >= self.chunk_size:
                    await self.close_current_file()

        except Exception as e:
            activity.logger.error(f"Error writing dataframe to parquet: {str(e)}")

    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to Parquet files.

        This method converts the daft DataFrame to pandas and writes it to Parquet files.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
        """
        # Convert daft DataFrame to pandas DataFrame for writing
        await self.write_dataframe(dataframe.to_pandas())

    async def close_current_file(self):
        """Close the current file and upload it to object store.

        This method closes the current file if one is open, uploads it to
        the object store, and optionally removes the local copy.
        """
        if not self.current_file:
            return

        self.current_file.close()
        await self.upload_file(self.current_file_name)
        # os.unlink(self.current_file_name)
        activity.logger.info(
            f"Uploaded file: {self.current_file_name} and removed local copy"
        )
        self.current_file = None
        self.current_file_name = None
        self.current_record_count = 0

    async def upload_file(self, local_file_path: str) -> None:
        """Upload a file to the object store.

        Args:
            local_file_path (str): Path to the local file to upload.
        """
        activity.logger.info(
            f"Uploading file: {local_file_path} to {self.output_prefix}"
        )
        await ObjectStoreOutput.push_file_to_object_store(
            self.output_prefix, local_file_path
        )

    async def _create_new_file(self):
        """Create a new Parquet file for writing.

        This method creates a new file with the current schema and initializes
        it for writing.
        """
        self.chunk_count += 1
        self.current_file_name = f"{self.output_path}_{self.chunk_count}.parquet"
        self.current_file = pq.ParquetWriter(
            self.current_file_name, self.schema, **self.parquet_writer_options
        )
        activity.logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0
