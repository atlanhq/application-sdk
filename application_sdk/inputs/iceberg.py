import math
from typing import Iterator, Optional

import daft
import pandas as pd
from pyiceberg.table import Table

from application_sdk import logging
from application_sdk.inputs import Input

logger = logging.get_logger(__name__)


class IcebergInput(Input):
    """
    Iceberg Input class to read data from Iceberg tables using daft and pandas
    """

    table: Table
    chunk_size: Optional[int]

    def __init__(self, table: Table, chunk_size: Optional[int] = 100000):
        self.table = table
        self.chunk_size = chunk_size

    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        """
        Method to read the data from the iceberg table
        and return as a batched pandas dataframe
        """
        try:
            for daft_df in self.get_batched_daft_dataframe():
                yield daft_df.to_pandas()
        except Exception as e:
            logger.error(f"Error reading batched data from Iceberg table: {str(e)}")

    def get_dataframe(self) -> pd.DataFrame:
        """
        Method to read the data from the iceberg table
        and return as a single combined pandas dataframe
        """
        try:
            daft_dataframe = self.get_daft_dataframe()
            return daft_dataframe.to_pandas()
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table: {str(e)}")

    def get_batched_daft_dataframe(self) -> Iterator[daft.DataFrame]:
        """
        Method to read the data from the iceberg table
        and return as a batched daft dataframe
        """
        try:
            iceberg_table_daft_df = daft.read_iceberg(self.table)
            partition_count: int = math.ceil(
                iceberg_table_daft_df.count_rows() / self.chunk_size
            )
            # divide the table into chunks and return them
            partitioned_df = iceberg_table_daft_df.into_partitions(partition_count)
            for partition in partitioned_df.iter_partitions():
                # when a daft dataframe is partitioned, it is returned as a micro partition object
                # and it does not provide a method to convert it to a daft dataframe
                # hence converting it to a dictionary and then to a daft dataframe
                yield daft.from_pydict(partition.to_pydict())

        except Exception as e:
            logger.error(
                f"Error reading batched data from Iceberg table using daft: {str(e)}"
            )

    def get_daft_dataframe(self) -> daft.DataFrame:
        """
        Method to read the data from the iceberg table
        and return as a single combined daft dataframe
        """
        try:
            return daft.read_iceberg(self.table)
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table using daft: {str(e)}")
