from abc import ABC, abstractmethod

import daft
import pandas as pd

from application_sdk import logging
from application_sdk.inputs.objectstore import ObjectStore

logger = logging.get_logger(__name__)


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

    async def write_metadata(self, file_suffix: str = None):
        """
        Method to write the metadata to a json file and push it to the object store
        """
        try:
            # prepare the metadata
            metadata = {
                "total_record_count": [self.total_record_count],
                "chunk_count": [self.chunk_count],
            }

            # If a suffix is provided, format it by replacing slashes with underscores
            if file_suffix:
                # Replace slashes with underscores to avoid issues
                suffix_part = f"_{file_suffix.replace('/', '_')}"
            else:
                suffix_part = ""

            # Write the metadata to a json file
            output_file_name = f"{self.output_path}/metadata{suffix_part}.json"
            df = pd.DataFrame(metadata)
            df.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.upload_file_prefix, output_file_name
            )
        except Exception as e:
            logger.error(f"Error writing metadata: {str(e)}")
