from typing import Any, Dict, Iterator, Optional

from pyiceberg.table import Table

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input

logger = get_logger(__name__)


class IcebergInput(Input):
    """
    Iceberg Input class to read data from Iceberg tables using daft and pandas
    """

    table: Table
    chunk_size: Optional[int]

    def __init__(
        self, table: Table, chunk_size: Optional[int] = 100000, **kwargs: Dict[str, Any]
    ):
        """Initialize the Iceberg input class.

        Args:
            table (Table): Iceberg table object.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
            kwargs (Dict[str, Any]): Keyword arguments for initialization.
        """
        self.table = table
        self.chunk_size = chunk_size

    def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the iceberg table
        and return as a single combined pandas dataframe
        """
        try:
            daft_dataframe = self.get_daft_dataframe()
            return daft_dataframe.to_pandas()
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table: {str(e)}")

    def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:
        # We are not implementing this method as we have to parition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the paritions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError

    def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the iceberg table
        and return as a single combined daft dataframe
        """
        try:
            import daft

            return daft.read_iceberg(self.table)
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table using daft: {str(e)}")

    def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:  # noqa: F821
        # We are not implementing this method as we have to parition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the paritions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError
