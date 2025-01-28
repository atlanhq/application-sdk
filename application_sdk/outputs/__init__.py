"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import logging
from abc import ABC, abstractmethod

import daft
import pandas as pd

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class Output(ABC):
    """Abstract base class for output handlers.

    This class defines the interface for output handlers that can write data
    to various destinations in different formats.

    Attributes:
        output_path (str): Path where the output will be written.
        upload_file_prefix (str): Prefix for files when uploading to object store.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks the output was split into.
    """

    output_path: str
    upload_file_prefix: str
    total_record_count: int
    chunk_count: int

    @abstractmethod
    async def write_df(self, df: pd.DataFrame):
        """Write a pandas DataFrame to the output destination.

        Args:
            df (pd.DataFrame): The DataFrame to write.
        """
        pass

    @abstractmethod
    async def write_daft_df(self, df: daft.DataFrame):
        """Write a daft DataFrame to the output destination.

        Args:
            df (daft.DataFrame): The DataFrame to write.
        """
        pass

    async def write_metadata(self):
        """Write metadata about the output to a JSON file.

        This method writes metadata including total record count and chunk count
        to a JSON file and uploads it to the object store.

        Raises:
            Exception: If there's an error writing or uploading the metadata.
        """
        try:
            # prepare the metadata
            metadata = {
                "total_record_count": [self.total_record_count],
                "chunk_count": [self.chunk_count],
            }

            # Write the metadata to a json file
            output_file_name = f"{self.output_path}/metadata.json.ignore"
            df = pd.DataFrame(metadata)
            df.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.upload_file_prefix, output_file_name
            )
        except Exception as e:
            logger.error(f"Error writing metadata: {str(e)}")
