from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator, Iterator, Union

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd


class Input(ABC):
    """
    Abstract base class for input data sources.
    """

    @abstractmethod
    async def get_batched_dataframe(
        self,
    ) -> Union[Iterator["pd.DataFrame"], AsyncIterator["pd.DataFrame"]]:
        """
        Get an iterator of batched pandas DataFrames.

        Returns:
            Iterator["pd.DataFrame"]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Get a single pandas DataFrame.

        Returns:
            "pd.DataFrame": A pandas DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_batched_daft_dataframe(
        self,
    ) -> Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:  # noqa: F821
        """
        Get an iterator of batched daft DataFrames.

        Returns:
            Iterator[daft.DataFrame]: An iterator of batched daft DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Get a single daft DataFrame.

        Returns:
            daft.DataFrame: A daft DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError
