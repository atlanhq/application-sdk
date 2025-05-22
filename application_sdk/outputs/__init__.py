"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import inspect
from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Generator,
    List,
    Optional,
    Union,
    cast,
)

import orjson
from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.objectstore import ObjectStoreOutput

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft
    import pandas as pd


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

    def process_null_fields(
        self,
        obj: Any,
        preserve_fields: Optional[List[str]] = None,
        null_to_empty_dict_fields: Optional[List[str]] = None,
    ) -> Any:
        """
        By default the method removes null values from dictionaries and lists.
        Except for the fields specified in preserve_fields.
        And fields in null_to_empty_dict_fields are replaced with empty dict if null.

        Args:
            obj: The object to clean (dict, list, or other value)
            preserve_fields: Optional list of field names that should be preserved even if they contain null values
            null_to_empty_dict_fields: Optional list of field names that should be replaced with empty dict if null

        Returns:
            The cleaned object with null values removed
        """
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                # Handle null fields that should be converted to empty dicts
                if k in (null_to_empty_dict_fields or []) and v is None:
                    result[k] = {}
                    continue

                # Process the value recursively
                processed_value = self.process_null_fields(
                    v, preserve_fields, null_to_empty_dict_fields
                )

                # Keep the field if it's in preserve_fields or has a non-None processed value
                if k in (preserve_fields or []) or processed_value is not None:
                    result[k] = processed_value

            return result
        return obj

    async def write_batched_dataframe(
        self,
        batched_dataframe: Union[
            AsyncGenerator["pd.DataFrame", None], Generator["pd.DataFrame", None, None]
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
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["pd.DataFrame", None, None], batched_dataframe
                )
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
        except Exception as e:
            logger.error(f"Error writing batched dataframe: {str(e)}")

    @abstractmethod
    async def write_dataframe(self, dataframe: "pd.DataFrame"):
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
                # Cast to Generator since we've confirmed it's not an AsyncGenerator
                sync_generator = cast(
                    Generator["daft.DataFrame", None, None], batched_dataframe
                )  # noqa: F821
                for dataframe in sync_generator:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
        except Exception as e:
            logger.error(f"Error writing batched daft dataframe: {str(e)}")

    @abstractmethod
    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to the output destination.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.
        """
        pass

    async def get_statistics(
        self, typename: Optional[str] = None
    ) -> ActivityStatistics:
        """Returns statistics about the output.

        This method returns a ActivityStatistics object with total record count and chunk count.

        Args:
            typename (str): Type name of the entity e.g database, schema, table.

        Raises:
            ValidationError: If the statistics data is invalid
            Exception: If there's an error writing the statistics
        """
        try:
            statistics = await self.write_statistics()
            if not statistics:
                raise ValueError("No statistics data available")
            statistics = ActivityStatistics.model_validate(statistics)
            if typename:
                statistics.typename = typename
            return statistics
        except Exception as e:
            logger.error(f"Error getting statistics: {str(e)}")
            raise

    async def write_statistics(self) -> Optional[Dict[str, Any]]:
        """Write statistics about the output to a JSON file.

        This method writes statistics including total record count and chunk count
        to a JSON file and uploads it to the object store.

        Raises:
            Exception: If there's an error writing or uploading the statistics.
        """
        try:
            # prepare the statistics
            statistics = {
                "total_record_count": self.total_record_count,
                "chunk_count": self.chunk_count,
            }

            # Write the statistics to a json file
            output_file_name = f"{self.output_path}/statistics.json.ignore"
            with open(output_file_name, "w") as f:
                f.write(orjson.dumps(statistics).decode("utf-8"))

            # Push the file to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                self.output_prefix, output_file_name
            )
            return statistics
        except Exception as e:
            logger.error(f"Error writing statistics: {str(e)}")
