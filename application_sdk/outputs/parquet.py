import os
from typing import TYPE_CHECKING, Literal, Optional

from temporalio import activity

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.outputs import Output
from application_sdk.outputs.objectstore import ObjectStoreOutput

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft
    import pandas as pd


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
        chunk_start (Optional[int]): Starting index for chunk numbering.
        path_gen (Callable): Function to generate file paths.
        start_marker (Optional[str]): Start marker for query extraction.
        end_marker (Optional[str]): End marker for query extraction.
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
        chunk_start: Optional[int] = None,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
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
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            path_gen (Callable, optional): Function to generate file paths.
                Defaults to path_gen function.
            start_marker (Optional[str], optional): Start marker for query extraction.
                Defaults to None.
            end_marker (Optional[str], optional): End marker for query extraction.
                Defaults to None.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.write_mode = write_mode
        self.chunk_size = chunk_size
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.chunk_start = chunk_start
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.metrics = get_metrics()

        # Create output directory
        self.output_path = os.path.join(self.output_path, self.output_suffix)
        if self.typename:
            self.output_path = os.path.join(self.output_path, self.typename)
        os.makedirs(self.output_path, exist_ok=True)

    def path_gen(
        self,
        chunk_start: int | None = None,
        chunk_count: int = 0,
        start_marker: Optional[str] = None,
        end_marker: Optional[str] = None,
    ) -> str:
        """Generate a file path for a chunk.

        Args:
            chunk_start (int | None): Starting index of the chunk, or None for single chunk.
            chunk_count (int): Total number of chunks.
            start_marker (Optional[str]): Start marker for query extraction.
            end_marker (Optional[str]): End marker for query extraction.

        Returns:
            str: Generated file path for the chunk.
        """
        # For Query Extraction - use start and end markers without chunk count
        if start_marker and end_marker:
            return f"{start_marker}_{end_marker}.parquet"

        # For regular chunking - include chunk count
        if chunk_start is None:
            return f"{str(chunk_count)}.parquet"
        else:
            return f"{str(chunk_start+chunk_count)}.parquet"

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
            file_path = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count, self.start_marker, self.end_marker)}"

            # Write the dataframe to parquet using pandas native method
            dataframe.to_parquet(
                file_path,
                index=False,
                compression="snappy",  # Using snappy compression by default
            )

            # Record metrics for successful write
            self.metrics.record_metric(
                name="parquet_write_records",
                value=len(dataframe),
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": self.write_mode},
                description="Number of records written to Parquet files from pandas DataFrame",
            )

            # Record chunk metrics
            self.metrics.record_metric(
                name="parquet_chunks_written",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": self.write_mode},
                description="Number of chunks written to Parquet files",
            )

            # Upload the file to object store
            await self.upload_file(file_path)
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="parquet_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "pandas", "mode": self.write_mode, "error": str(e)},
                description="Number of errors while writing to Parquet files",
            )
            logger.error(f"Error writing pandas dataframe to parquet: {str(e)}")
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

            # Generate file path using path_gen function
            file_path = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count, self.start_marker, self.end_marker)}"

            # Write the dataframe to parquet using daft
            dataframe.write_parquet(
                file_path,
                write_mode=self.write_mode,
            )

            # Record metrics for successful write
            self.metrics.record_metric(
                name="parquet_write_records",
                value=row_count,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "mode": self.write_mode},
                description="Number of records written to Parquet files from daft DataFrame",
            )

            # Record chunk metrics
            self.metrics.record_metric(
                name="parquet_chunks_written",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "mode": self.write_mode},
                description="Number of chunks written to Parquet files",
            )

            # Upload the file to object store
            await self.upload_file(file_path)
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="parquet_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"type": "daft", "mode": self.write_mode, "error": str(e)},
                description="Number of errors while writing to Parquet files",
            )
            logger.error(f"Error writing daft dataframe to parquet: {str(e)}")
            raise

    async def upload_file(self, local_file_path: str) -> None:
        """Upload a file to the object store.

        Args:
            local_file_path (str): Path to the local file to upload.
        """
        try:
            if os.path.isdir(local_file_path):
                logger.info(
                    f"Uploading files: {local_file_path} to {self.output_prefix}"
                )
                await ObjectStoreOutput.push_files_to_object_store(
                    self.output_prefix, local_file_path
                )
            else:
                logger.info(
                    f"Uploading file: {local_file_path} to {self.output_prefix}"
                )
                await ObjectStoreOutput.push_file_to_object_store(
                    self.output_prefix, local_file_path
                )
        except Exception as e:
            # Record metrics for failed upload
            self.metrics.record_metric(
                name="parquet_upload_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"error": str(e)},
                description="Number of errors while uploading Parquet files to object store",
            )
            logger.error(f"Error uploading file to object store: {str(e)}")
            raise e

    def get_full_path(self) -> str:
        """Get the full path of the output file.

        Returns:
            str: The full path of the output file.
        """
        return self.output_path
