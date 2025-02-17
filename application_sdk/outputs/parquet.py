import os
from typing import Any, Dict, List, Optional, Union, cast

import pandas as pd
import pyarrow as pa  # type: ignore
import pyarrow.parquet as pq  # type: ignore
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.outputs import Output
from application_sdk.outputs.objectstore import ObjectStoreOutput

activity.logger = get_logger(__name__)


class ParquetOutput(Output):
    """Output handler for writing data to Parquet files.

    This class handles writing DataFrames to Parquet files with support for chunking,
    schema evolution, and automatic uploading to object store.

    Attributes:
        output_path (str): Path where Parquet files will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks created.
        schema (Optional[pa.Schema]): Current schema for Parquet files.
    """

    def __init__(
        self,
        output_suffix: str,
        output_path: str,
        output_prefix: str,
        typename: Optional[str] = None,
        state: Optional[ActivitiesState] = None,
        chunk_size: int = 100000,
        total_record_count: int = 0,
        chunk_count: int = 0,
        schema: Optional[Any] = None,  # type: ignore # pa.Schema not recognized by type checker
        parquet_writer_options: Dict[str, Any] = {},
        **kwargs: Dict[str, Any],
    ):
        """Initialize the Parquet output handler.

        Args:
            output_path (str): Path where Parquet files will be written.
            output_suffix (str): Suffix for output files.
            output_prefix (str): Prefix for files where the files will be written and uploaded.
            typename (Optional[str], optional): Type name of the entity e.g database, schema, table.
            state (Optional[ActivitiesState], optional): State of the activities.
            chunk_size (int, optional): Maximum number of records per chunk.
                Defaults to 100000.
            total_record_count (int, optional): Initial total record count.
                Defaults to 0.
            chunk_count (int, optional): Initial chunk count.
                Defaults to 0.
            schema (Optional[pa.Schema], optional): Initial schema for Parquet files.
                Defaults to None.
            parquet_writer_options (Dict[str, Any], optional): Options for Parquet writer.
                Defaults to {}.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.state = state
        self.chunk_size = chunk_size
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.schema = schema
        self.parquet_writer_options = parquet_writer_options

    @classmethod
    def re_init(
        cls,
        output_path: str,
        typename: Optional[str] = None,
        chunk_count: int = 0,
        total_record_count: int = 0,
        output_suffix: str = "",
        **kwargs: Dict[str, Any],
    ):
        """Re-initialize the output class with given keyword arguments.

        Args:
            output_path (str): Path where Parquet files will be written.
            typename (str, optional): Type name of the entity e.g database, schema, table.
                Defaults to None.
            chunk_count (int, optional): Initial chunk count.
                Defaults to 0.
            total_record_count (int, optional): Initial total record count.
                Defaults to 0.
            output_suffix (str, optional): Suffix for output files.
                Defaults to empty string.
            kwargs (Dict[str, Any]): Additional keyword arguments.
        """
        output_path = f"{output_path}{output_suffix}"
        if typename:
            output_path = f"{output_path}/{typename}"
        os.makedirs(f"{output_path}", exist_ok=True)

        # Extract output_prefix from kwargs or use output_path as default
        output_prefix = str(kwargs.pop("output_prefix", output_path))

        # Extract known parameters from kwargs
        chunk_size = int(kwargs.pop("chunk_size", 100000))
        state = cast(Optional[ActivitiesState], kwargs.pop("state", None))

        return cls(
            output_suffix=output_suffix,
            output_path=output_path,
            output_prefix=output_prefix,
            typename=typename,
            chunk_count=chunk_count,
            total_record_count=total_record_count,
            chunk_size=chunk_size,
            state=state,
            **kwargs,
        )

    async def update_schema(self, new_schema: Any):  # type: ignore # pa.Schema not recognized
        """Update the schema by merging with a new schema.

        This method handles schema evolution by merging the existing schema
        with new fields from the provided schema.

        Args:
            new_schema (pa.Schema): New schema to merge with existing one.
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

        This method writes the DataFrame to Parquet files, potentially splitting it
        into chunks based on chunk_size settings.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        if len(dataframe) == 0:
            return

        try:
            # Split the DataFrame into chunks
            chunks = [
                dataframe[i : i + self.chunk_size]
                for i in range(0, len(dataframe), self.chunk_size)
            ]

            for chunk in chunks:
                # Convert chunk to Arrow table and update schema
                table = pa.Table.from_pandas(chunk)  # type: ignore
                await self.update_schema(table.schema)

                # Write chunk to Parquet file
                self.chunk_count += 1
                self.total_record_count += len(chunk)
                output_file_name = f"{self.output_path}/{self.chunk_count}.parquet"

                pq.write_table(  # type: ignore
                    table.cast(self.schema),
                    output_file_name,
                    **self.parquet_writer_options,
                )

                # Push the file to the object store
                await ObjectStoreOutput.push_file_to_object_store(
                    self.output_prefix, output_file_name
                )

        except Exception as e:
            activity.logger.error(f"Error writing dataframe to parquet: {str(e)}")

    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to Parquet files.

        This method converts the daft DataFrame to pandas and writes it to Parquet files.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.

        Note:
            Daft has built-in Parquet writing support, but we convert to pandas
            to maintain consistency with schema evolution and chunking.
        """
        await self.write_dataframe(dataframe.to_pandas())
