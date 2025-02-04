"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, Generator, Optional, Union

import pandas as pd
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


def is_empty_dataframe(dataframe: Union[pd.DataFrame, "daft.DataFrame"]) -> bool:  # noqa: F821
    """
    Helper method to check if the dataframe has any rows
    """
    if isinstance(dataframe, pd.DataFrame):
        return dataframe.empty

    try:
        import daft

        if isinstance(dataframe, daft.DataFrame):
            return dataframe.count_rows() == 0
    except Exception:
        activity.logger.warning("Module daft not found")
    return True


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
    output_prefix: str
    total_record_count: int
    chunk_count: int
    state: Optional[ActivitiesState] = None

    @classmethod
    def re_init(cls, **kwargs: Dict[str, Any]):
        """Re-initialize the output class with given keyword arguments.

        Args:
            **kwargs (Dict[str, Any]): Keyword arguments for re-initialization.
        """
        return cls(**kwargs)

    async def write_batched_dataframe(
        self,
        batched_dataframe: Union[
            AsyncGenerator[pd.DataFrame, None], Generator[pd.DataFrame, None, None]
        ],
    ):
        """Write a batched pandas DataFrame to Output.

        This method writes the DataFrame to Output provided, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
            else:
                for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
        except Exception as e:
            activity.logger.error(f"Error writing batched dataframe to json: {str(e)}")

    @abstractmethod
    async def write_dataframe(self, dataframe: pd.DataFrame):
        """Write a pandas DataFrame to the output destination.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.
        """
        pass

    async def write_batched_daft_dataframe(
        self,
        batched_dataframe: Union[
            AsyncGenerator["daft.DataFrame", None],  # noqa: F821
            Generator["daft.DataFrame", None, None],  # noqa: F821
        ],
    ):
        """Write a batched daft DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
            else:
                for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
        except Exception as e:
            activity.logger.error(
                f"Error writing batched daft dataframe to json: {str(e)}"
            )

    @abstractmethod
    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to the output destination.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
        """
        pass

    def get_statistics(self) -> Any:
        """Get statistics about the output."""
        pass

    async def write_statistics(self) -> Optional[Dict[str, Any]]:
        """Write metadata about the output to a JSON file.

        This method writes statistics including total record count and chunk count
        to a JSON file and uploads it to the object store.

        Raises:
            Exception: If there's an error writing or uploading the metadata.
        """
        try:
            # prepare the metadata
            statistics = {
                "total_record_count": self.total_record_count,
                "chunk_count": self.chunk_count,
            }

            # Write the statistics to a json file
            output_file_name = f"{self.output_path}/metadata.json.ignore"
            dataframe = pd.DataFrame(statistics, index=[0])
            dataframe.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.output_prefix, output_file_name
            )
            return statistics
        except Exception as e:
            activity.logger.error(f"Error writing statistics: {str(e)}")
