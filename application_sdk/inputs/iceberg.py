from typing import TYPE_CHECKING, AsyncIterator, Iterator, Optional, Union

from pyiceberg.table import Table

from application_sdk.inputs import Input
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd


class IcebergInput(Input):
    """
    Iceberg Input class to read data from Iceberg tables using daft and pandas
    """

    table: Table
    chunk_size: Optional[int]

    def __init__(self, table: Table, chunk_size: Optional[int] = 100000):
        """Initialize the Iceberg input class.

        Args:
            table (Table): Iceberg table object.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
        """
        self.table = table
        self.chunk_size = chunk_size

    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the iceberg table
        and return as a single combined pandas dataframe
        """
        try:
            daft_dataframe = await self.get_daft_dataframe()
            return daft_dataframe.to_pandas()
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table: {str(e)}")
            raise

    async def get_batched_dataframe(
        self,
    ) -> Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]:
        # We are not implementing this method as we have to partition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the partitions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the iceberg table
        and return as a single combined daft dataframe
        """
        try:
            import daft

            return daft.read_iceberg(self.table)
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table using daft: {str(e)}")
            raise

    async def get_batched_daft_dataframe(
        self,
    ) -> Union[AsyncIterator["daft.DataFrame"], Iterator["daft.DataFrame"]]:  # noqa: F821
        # We are not implementing this method as we have to partition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the partitions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError
