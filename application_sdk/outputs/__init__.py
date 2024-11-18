from abc import abstractmethod, ABC
import pandas as pd
import os

from application_sdk import logging

logger = logging.get_logger(__name__)

class Output(ABC):
    @abstractmethod
    def write_df(self, df: pd.DataFrame, chunk_num: int=0):
        pass

class JsonOutput(Output):
    def __init__(self, output_path: str):
        self.output_path = output_path
        os.makedirs(f"{output_path}", exist_ok=True)

    def write_df(self, df: pd.DataFrame, chunk_num: int=0):
        df.to_json(f"{self.output_path}/{chunk_num}.json", orient="records", lines=True)
