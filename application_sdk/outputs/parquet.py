import os
from typing import Any, List, Literal, Optional

import daft
import pandas as pd
from temporalio import activity

from application_sdk.common.error_codes import ApplicationFrameworkErrorCodes
from application_sdk.common.logger_adaptors import get_logger
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
        output_path: str = "",
        output_suffix: str = "",
        output_prefix: str = "",
        typename: Optional[str] = None,
        write_mode: Literal["append", "overwrite", "overwrite-partitions"] = "append",
        chunk_size: Optional[int] = 100000,
        total_record_count: int = 0,
        chunk_count: int = 0,
    ):
        """Initialize the Parquet output handler.

        Args:
            output_path (str): Base path where Parquet files will be written.
            output_suffix (str): Suffix for output files.
            output_prefix (str): Prefix for files when uploading to object store.
            typename (Optional[str], optional): Type name of the entity e.g database, schema, table.
            mode (str, optional): Write mode for parquet files. Defaults to "append".
            chunk_size (int, optional): Maximum records per chunk. Defaults to 100000.
            total_record_count (int, optional): Initial total record count. Defaults to 0.
            chunk_count (int, optional): Initial chunk count. Defaults to 0.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.write_mode = write_mode
        self.chunk_size = chunk_size
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count

        # Create output directory
        self.output_path = os.path.join(self.output_path, self.output_suffix)
        if self.typename:
            self.output_path = os.path.join(self.output_path, self.typename)
        os.makedirs(self.output_path, exist_ok=True)

    async def write_dataframe(self, dataframe: "pd.DataFrame"):
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
            file_path = f"{self.output_path}/{self.chunk_count}.parquet"

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
                f"Error writing pandas dataframe to parquet: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.OutputErrorCodes.PARQUET_WRITE_ERROR,
            )
            raise

    async def write_daft_dataframe(self, dataframe: daft.DataFrame):  # noqa: F821
        """Write a daft DataFrame to Parquet files and upload to object store.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
        """
        try:
            row_count = dataframe.count_rows()
            if row_count == 0:
                return

            # Update counters
            self.chunk_count += 1
            self.total_record_count += row_count

            # Write the dataframe to parquet using daft
            dataframe.write_parquet(
                self.output_path,
                write_mode=self.write_mode,
            )

            # Upload the file to object store
            await self.upload_file(self.output_path)
        except Exception as e:
            activity.logger.error(
                f"Error writing daft dataframe to parquet: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.OutputErrorCodes.PARQUET_WRITE_ERROR,
            )
            raise

    async def upload_file(self, local_file_path: str) -> None:
        """Upload a file to the object store.

        Args:
            local_file_path (str): Path to the local file to upload.
        """
        activity.logger.info(
            f"Uploading file: {local_file_path} to {self.output_prefix}"
        )
        await ObjectStoreOutput.push_files_to_object_store(
            self.output_prefix, local_file_path
        )

    @classmethod
    async def write_dataframe_to_parquet(
        cls, df, output_path: str, file_name: str = "output.parquet"
    ) -> None:
        """Writes a DataFrame to a Parquet file.

        Args:
            df: The DataFrame to write
            output_path (str): The directory path where the Parquet file will be written
            file_name (str, optional): The name of the Parquet file. Defaults to "output.parquet"

        Raises:
            Exception: If there's an error writing the DataFrame to Parquet
        """
        try:
            # Create output directory if it doesn't exist
            os.makedirs(output_path, exist_ok=True)

            # Write to Parquet file
            file_path = os.path.join(output_path, file_name)
            df.to_parquet(file_path)

            activity.logger.info(f"Successfully wrote DataFrame to {file_path}")

        except Exception as e:
            activity.logger.error(
                f"Error writing DataFrame to Parquet: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.OutputErrorCodes.PARQUET_WRITE_ERROR,
            )
            raise e

    @classmethod
    async def write_batched_dataframes_to_parquet(
        cls, dfs: List[Any], output_path: str, file_prefix: str = "output"
    ) -> None:
        """Writes multiple DataFrames to separate Parquet files.

        Args:
            dfs (List[Any]): List of DataFrames to write
            output_path (str): The directory path where the Parquet files will be written
            file_prefix (str, optional): Prefix for the Parquet files. Defaults to "output"

        Raises:
            Exception: If there's an error writing the DataFrames to Parquet
        """
        try:
            for i, df in enumerate(dfs):
                file_name = f"{file_prefix}_{i}.parquet"
                await cls.write_dataframe_to_parquet(df, output_path, file_name)

            activity.logger.info(
                f"Successfully wrote {len(dfs)} DataFrames to Parquet files"
            )

        except Exception as e:
            activity.logger.error(
                f"Error writing batched DataFrames to Parquet: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.OutputErrorCodes.PARQUET_BATCH_WRITE_ERROR,
            )
            raise e
