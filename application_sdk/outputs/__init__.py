"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, Generator, Optional, Union

import pandas as pd
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.outputs.objectstore import ObjectStoreOutput

activity.logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft


def is_empty_dataframe(dataframe: Union[pd.DataFrame, "daft.DataFrame"]) -> bool:
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

    output_path: str | None = None
    output_prefix: str | None = None
    total_record_count: int | None = None
    chunk_count: int | None = None
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
            batched_dataframe: A generator or async generator of DataFrames to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            # Handle based on type - first check AsyncGenerator specifically
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
            # Handle sync generators with regular for
            elif inspect.isgenerator(batched_dataframe):
                for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
            # Handle single dataframe case
            elif isinstance(batched_dataframe, pd.DataFrame):
                if not is_empty_dataframe(batched_dataframe):
                    await self.write_dataframe(batched_dataframe)
            # Handle regular iterables only (not AsyncGenerator)
            elif hasattr(batched_dataframe, "__iter__") and not isinstance(
                batched_dataframe, AsyncGenerator
            ):
                for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_dataframe(dataframe)
            # Unknown type
            else:
                activity.logger.warning(
                    f"Unsupported dataframe type: {type(batched_dataframe)}"
                )
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
            batched_dataframe: A generator or async generator of daft DataFrames to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            # Try to import daft to check instance type
            try:
                import daft

                has_daft = True
            except ImportError:
                has_daft = False

            # Handle based on type - first check AsyncGenerator specifically
            if inspect.isasyncgen(batched_dataframe):
                async for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
            # Handle sync generators with regular for
            elif inspect.isgenerator(batched_dataframe):
                for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
            # Handle single dataframe case
            elif has_daft and isinstance(batched_dataframe, daft.DataFrame):
                if not is_empty_dataframe(batched_dataframe):
                    await self.write_daft_dataframe(batched_dataframe)
            # Handle regular iterables only (not AsyncGenerator)
            elif hasattr(batched_dataframe, "__iter__") and not isinstance(
                batched_dataframe, AsyncGenerator
            ):
                for dataframe in batched_dataframe:
                    if not is_empty_dataframe(dataframe):
                        await self.write_daft_dataframe(dataframe)
            # Unknown type
            else:
                activity.logger.warning(
                    f"Unsupported dataframe type: {type(batched_dataframe)}"
                )
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
            activity.logger.error(f"Error getting statistics: {str(e)}")
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
            dataframe = pd.DataFrame(statistics, index=[0])
            dataframe.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                self.output_prefix, output_file_name
            )
            return statistics
        except Exception as e:
            activity.logger.error(f"Error writing statistics: {str(e)}")
