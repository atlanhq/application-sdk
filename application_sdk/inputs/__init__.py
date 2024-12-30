import abc
from abc import abstractmethod
from typing import Iterator

import daft
import pandas as pd

from application_sdk import logging

logger = logging.get_logger(__name__)


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
