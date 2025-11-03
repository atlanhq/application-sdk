from typing import TYPE_CHECKING, Union

from pyiceberg.catalog import Catalog
from pyiceberg.table import Table
from temporalio import activity

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.outputs import Output

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft
    import pandas as pd


class IcebergOutput(Output):
    """
    Iceberg Output class to write data to Iceberg tables using daft and pandas
    """

    def __init__(
        self,
        iceberg_catalog: Catalog,
        iceberg_namespace: str,
        iceberg_table: Union[str, Table],
        mode: str = "append",
        total_record_count: int = 0,
        chunk_count: int = 0,
    ):
        """Initialize the Iceberg output class.

        Args:
            iceberg_catalog (Catalog): Iceberg catalog object.
            iceberg_namespace (str): Iceberg namespace.
            iceberg_table (Union[str, Table]): Iceberg table object or table name.
            mode (str, optional): Write mode for the iceberg table. Defaults to "append".
            total_record_count (int, optional): Total record count written to the iceberg table. Defaults to 0.
            chunk_count (int, optional): Number of chunks written to the iceberg table. Defaults to 0.
        """
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.iceberg_catalog = iceberg_catalog
        self.iceberg_namespace = iceberg_namespace
        self.iceberg_table = iceberg_table
        self.mode = mode
        self.metrics = get_metrics()

    async def write_dataframe(self, dataframe: "pd.DataFrame"):
        """
        Method to write the pandas dataframe to an iceberg table
        """
        try:
            import daft

            if len(dataframe) == 0:
                return
            # convert the pandas dataframe to a daft dataframe
            daft_dataframe = daft.from_pandas(dataframe)
            await self.write_daft_dataframe(daft_dataframe)

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

    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
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
