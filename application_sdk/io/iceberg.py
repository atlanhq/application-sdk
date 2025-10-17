from typing import TYPE_CHECKING, AsyncIterator, Optional, Union

from pyiceberg.catalog import Catalog
from pyiceberg.table import Table
from temporalio import activity

from application_sdk.io import DataframeType, Reader, Writer
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft
    import pandas as pd


class IcebergReader(Reader):
    """
    Iceberg Input class to read data from Iceberg tables using daft and pandas
    """

    table: Table
    chunk_size: Optional[int]

    def __init__(
        self,
        table: Table,
        chunk_size: Optional[int] = 100000,
        df_type: DataframeType = DataframeType.pandas,
    ):
        """Initialize the Iceberg input class.

        Args:
            table (Table): Iceberg table object.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
        """
        self.table = table
        self.chunk_size = chunk_size
        self.df_type = df_type

    async def read(self) -> "daft.DataFrame":
        """
        Method to read the data from the iceberg table
        and return as a single combined daft dataframe
        """
        try:
            if self.df_type == DataframeType.daft:
                import daft

                return daft.read_iceberg(self.table)
            else:
                raise ValueError(f"Unsupported df_type: {self.df_type}")
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table using daft: {str(e)}")
            raise

    def read_batches(
        self,
    ) -> AsyncIterator["daft.DataFrame"]:  # noqa: F821
        # We are not implementing this method as we have to partition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the partitions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError


class IcebergWriter(Writer):
    """
    Iceberg Writer class to write data to Iceberg tables using daft and pandas
    """

    def __init__(
        self,
        iceberg_catalog: Catalog,
        iceberg_namespace: str,
        iceberg_table: Union[str, Table],
        mode: str = "append",
        total_record_count: int = 0,
        chunk_count: int = 0,
        retain_local_copy: bool = False,
        df_type: DataframeType = DataframeType.pandas,
    ):
        """Initialize the Iceberg writer class.

        Args:
            iceberg_catalog (Catalog): Iceberg catalog object.
            iceberg_namespace (str): Iceberg namespace.
            iceberg_table (Union[str, Table]): Iceberg table object or table name.
            mode (str, optional): Write mode for the iceberg table. Defaults to "append".
            total_record_count (int, optional): Total record count written to the iceberg table. Defaults to 0.
            chunk_count (int, optional): Number of chunks written to the iceberg table. Defaults to 0.
            retain_local_copy (bool, optional): Whether to retain the local copy of the files.
                Defaults to False.
        """
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.iceberg_catalog = iceberg_catalog
        self.iceberg_namespace = iceberg_namespace
        self.iceberg_table = iceberg_table
        self.mode = mode
        self.metrics = get_metrics()
        self.retain_local_copy = retain_local_copy
        self.df_type = df_type

    async def _write_dataframe(self, dataframe: "pd.DataFrame", **kwargs):
        """
        Method to write the pandas dataframe to an iceberg table
        """
        try:
            import daft

            if len(dataframe) == 0:
                return
            # convert the pandas dataframe to a daft dataframe
            daft_dataframe = daft.from_pandas(dataframe)
            await self._write_daft_dataframe(daft_dataframe)

            # Record metrics for successful write
            self.metrics.record_metric(
                name="iceberg_write_records",
                value=len(dataframe),
                metric_type=MetricType.COUNTER,
                labels={"mode": self.mode, "type": "pandas"},
                description="Number of records written to Iceberg table from pandas DataFrame",
            )
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="iceberg_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"mode": self.mode, "type": "pandas", "error": str(e)},
                description="Number of errors while writing to Iceberg table",
            )
            logger.error(f"Error writing pandas dataframe to iceberg table: {str(e)}")
            raise e

    async def _write_daft_dataframe(self, dataframe: "daft.DataFrame", **kwargs):  # noqa: F821
        """
        Method to write the daft dataframe to an iceberg table
        """
        try:
            if dataframe.count_rows() == 0:
                return
            # Create a new table in the iceberg catalog
            self.chunk_count += 1
            self.total_record_count += dataframe.count_rows()

            # check if iceberg table is already created
            if isinstance(self.iceberg_table, Table):
                # if yes, use the existing iceberg table
                table = self.iceberg_table
            else:
                # if not, create a new table in the iceberg catalog
                table = self.iceberg_catalog.create_table_if_not_exists(
                    f"{self.iceberg_namespace}.{self.iceberg_table}",
                    schema=dataframe.to_arrow().schema,
                )
            # write the dataframe to the iceberg table
            dataframe.write_iceberg(table, mode=self.mode)

            # Record metrics for successful write
            self.metrics.record_metric(
                name="iceberg_write_records",
                value=dataframe.count_rows(),
                metric_type=MetricType.COUNTER,
                labels={"mode": self.mode, "type": "daft"},
                description="Number of records written to Iceberg table from daft DataFrame",
            )

            # Record chunk metrics
            self.metrics.record_metric(
                name="iceberg_chunks_written",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"mode": self.mode},
                description="Number of chunks written to Iceberg table",
            )
        except Exception as e:
            # Record metrics for failed write
            self.metrics.record_metric(
                name="iceberg_write_errors",
                value=1,
                metric_type=MetricType.COUNTER,
                labels={"mode": self.mode, "type": "daft", "error": str(e)},
                description="Number of errors while writing to Iceberg table",
            )
            logger.error(f"Error writing daft dataframe to iceberg table: {str(e)}")
            raise e
