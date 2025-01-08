import abc
import logging
from abc import abstractmethod
from typing import Iterator

import daft
import pandas as pd

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class Input(abc.ABC):
    @abstractmethod
    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        raise NotImplementedError

    @abstractmethod
    def get_dataframe(self) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_batched_daft_dataframe(self) -> Iterator[daft.DataFrame]:
        raise NotImplementedError

    @abstractmethod
    def get_daft_dataframe(self) -> daft.DataFrame:
        raise NotImplementedError
