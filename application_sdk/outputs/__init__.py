import os
from abc import ABC, abstractmethod

import pandas as pd

from application_sdk import logging
from application_sdk.inputs.objectstore import ObjectStore

logger = logging.get_logger(__name__)


class Output(ABC):
    output_path: str
    upload_file_prefix: str
    typename: str
    total_record_count: int
    chunk_count: int

    @abstractmethod
    async def write_df(self, df: pd.DataFrame):
        pass

    async def write_metadata(self):
        """
        Method to write the metadata to a json file and push it to the object store
        """
        # prepare the metadata
        metadata = {
            "total_record_count": [self.total_record_count],
            "chunk_count": [self.chunk_count],
        }

        # Write the metadata to a json file
        output_file_name = f"{self.output_path}/{self.typename}-metadata.json"
        df = pd.DataFrame(metadata)
        df.to_json(output_file_name, orient="records", lines=True)

        # Push the file to the object store
        await ObjectStore.push_file_to_object_store(
            self.upload_file_prefix, output_file_name
        )


class JsonOutput(Output):
    def __init__(self, output_path: str, upload_file_prefix: str, typename: str):
        self.output_path = output_path
        self.upload_file_prefix = upload_file_prefix
        self.typename = typename
        self.total_record_count = 0
        self.chunk_count = 0
        os.makedirs(f"{output_path}", exist_ok=True)

    async def write_df(self, df: pd.DataFrame):
        """
        Method to write the dataframe to a json file and push it to the object store
        """
        self.chunk_count += 1
        self.total_record_count += len(df)

        # Write the dataframe to a json file
        output_file_name = (
            f"{self.output_path}/{self.typename}-{str(self.chunk_count)}.json"
        )
        df.to_json(output_file_name, orient="records", lines=True)

        # Push the file to the object store
        await ObjectStore.push_file_to_object_store(
            self.upload_file_prefix, output_file_name
        )
