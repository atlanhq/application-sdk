import os
from typing import Any, Dict, Optional

from temporalio import activity

from application_sdk.activities import ActivitiesState
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
        output_path: Optional[str] = None,
        output_suffix: Optional[str] = None,
        output_prefix: Optional[str] = None,
        typename: Optional[str] = None,
        mode: str = "append",
        chunk_size: Optional[int] = 100000,
        total_record_count: int = 0,
        chunk_count: int = 0,
        state: Optional[ActivitiesState] = None,
        **kwargs: Dict[str, Any],
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
            state (Optional[ActivitiesState], optional): State object for the activity.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.mode = mode
        self.chunk_size = chunk_size
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.state = state

        # Create output directory
        full_path = f"{output_path}{output_suffix}"
        if typename:
            full_path = f"{full_path}/{typename}"
        os.makedirs(full_path, exist_ok=True)

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
            row_count = dataframe.count_rows()
            if row_count == 0:
                return

            # Update counters
            self.chunk_count += 1
            self.total_record_count += row_count

            # Generate output file path
            file_path = f"{self.output_path}{self.output_suffix}"
            if self.typename:
                file_path = f"{file_path}{self.typename}"

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
        await ObjectStoreOutput.push_files_to_object_store(
            self.output_prefix, local_file_path
        )
