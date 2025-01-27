import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class Input(ABC):
    """
    Abstract base class for input data sources.
    """

    @classmethod
    async def re_init(cls, **kwargs: Dict[str, Any]):
        """
        Re-initialize the input class with given keyword arguments.

        Args:
            **kwargs (Dict[str, Any]): Keyword arguments for re-initialization.

        Returns:
            Input: An instance of the input class.
        """
        return cls(**kwargs)

    @abstractmethod
    def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:  # noqa: F821
        """
        Get an iterator of batched pandas DataFrames.

        Returns:
            Iterator[pd.DataFrame]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    def get_dataframe(self) -> "pd.DataFrame":  # noqa: F821
        """
        Get a single pandas DataFrame.

        Returns:
            pd.DataFrame: A pandas DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:  # noqa: F821
        """
        Get an iterator of batched daft DataFrames.

        Returns:
            Iterator[daft.DataFrame]: An iterator of batched daft DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Get a single daft DataFrame.

        Returns:
            daft.DataFrame: A daft DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError
