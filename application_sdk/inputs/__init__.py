import abc
from abc import abstractmethod
from typing import Any, Iterator

import pandas as pd

from application_sdk import logging

logger = logging.get_logger(__name__)


class Input(abc.ABC):
    @abstractmethod
    def get_batched_df(self) -> Iterator[pd.DataFrame]:
        raise NotImplementedError

    @abstractmethod
    def get_df(self) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_key(self, key: str) -> Any:
        raise NotImplementedError
