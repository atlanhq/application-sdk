import logging
from abc import ABC, abstractmethod

import daft
import pandas as pd

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.outputs.objectstore import ObjectStoreOutput

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class Output(ABC):
    output_path: str
    upload_file_prefix: str
    total_record_count: int
    chunk_count: int

    @abstractmethod
    async def write_df(self, df: pd.DataFrame):
        pass

    @abstractmethod
    async def write_daft_df(self, df: daft.DataFrame):
        pass

    async def write_metadata(self):
        """
        Method to write the metadata to a json file and push it to the object store
        """
        try:
            # prepare the metadata
            metadata = {
                "total_record_count": [self.total_record_count],
                "chunk_count": [self.chunk_count],
            }

            # Write the metadata to a json file
            output_file_name = f"{self.output_path}/metadata.json"
            df = pd.DataFrame(metadata)
            df.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                self.upload_file_prefix, output_file_name
            )
        except Exception as e:
            logger.error(f"Error writing metadata: {str(e)}")
