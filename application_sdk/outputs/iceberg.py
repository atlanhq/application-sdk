import logging
from typing import Any, Dict, Union

from pyiceberg.catalog import Catalog
from pyiceberg.table import Table
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.outputs import Output

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


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
        **kwargs: Dict[str, Any],
    ):
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.iceberg_catalog = iceberg_catalog
        self.iceberg_namespace = iceberg_namespace
        self.iceberg_table = iceberg_table
        self.mode = mode

    def re_init(
        self,
        chunk_count: int = 0,
        total_record_count: int = 0,
        **kwargs: Dict[str, Any],
    ):
        self.total_record_count = 0
        self.chunk_count = 0
        if chunk_count:
            self.chunk_count = chunk_count
        if total_record_count:
            self.total_record_count = total_record_count

    async def write_df(self, df: "pd.DataFrame"):  # noqa: F821
        """
        Method to write the pandas dataframe to an iceberg table
        """
        try:
            import daft

            if len(df) == 0:
                return
            # convert the pandas dataframe to a daft dataframe
            daft_df = daft.from_pandas(df)
            self.write_daft_df(daft_df)
        except Exception as e:
            activity.logger.error(
                f"Error writing pandas dataframe to iceberg table: {str(e)}"
            )

    async def write_daft_df(self, df: "daft.DataFrame"):  # noqa: F821
        """
        Method to write the daft dataframe to an iceberg table
        """
        try:
            if df.count_rows() == 0:
                return
            # Create a new table in the iceberg catalog
            self.chunk_count += 1
            self.total_record_count += df.count_rows()

            # check if iceberg table is already created
            if isinstance(self.iceberg_table, Table):
                # if yes, use the existing iceberg table
                table = self.iceberg_table
            else:
                # if not, create a new table in the iceberg catalog
                table = self.iceberg_catalog.create_table_if_not_exists(
                    f"{self.iceberg_namespace}.{self.iceberg_table}",
                    schema=df.to_arrow().schema,
                )
            # write the dataframe to the iceberg table
            df.write_iceberg(table, mode=self.mode)
        except Exception as e:
            activity.logger.error(
                f"Error writing daft dataframe to iceberg table: {str(e)}"
            )
