"""Output module for handling data output operations.

This module provides base classes and utilities for handling various types of data outputs
in the application, including file outputs and object store interactions.
"""

import inspect
from abc import ABC, abstractmethod
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Generator,
    List,
    Literal,
    Optional,
    Union,
    cast,
)

import orjson
from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import get_object_store_prefix, build_output_path
from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore
from application_sdk.constants import TEMPORARY_PATH

logger = get_logger(__name__)
activity.logger = logger


if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class WorkflowPhase(Enum):
    """Enumeration of workflow phases for data processing."""
    
    EXTRACT = "Extract"
    TRANSFORM = "Transform" 
    PUBLISH = "Publish"


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
    phase: WorkflowPhase
    total_record_count: int
    chunk_count: int
    statistics: List[int] = []

    def estimate_dataframe_file_size(
        self, dataframe: "pd.DataFrame", file_type: Literal["json", "parquet"]
    ) -> int:
        """Estimate File size of a DataFrame by sampling a few records."""
        if len(dataframe) == 0:
            return 0

        # Sample up to 10 records to estimate average size
        sample_size = min(10, len(dataframe))
        sample = dataframe.head(sample_size)
        if file_type == "json":
            sample_file = sample.to_json(orient="records", lines=True)
        else:
            sample_file = sample.to_parquet(index=False, compression="snappy")
        if sample_file is not None:
            avg_record_size = len(sample_file) / sample_size
            return int(avg_record_size * len(dataframe))

        return 0

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
            statistics = await self.write_statistics(typename)
            if not statistics:
                raise ValueError("No statistics data available")
            statistics = ActivityStatistics.model_validate(statistics)
            if typename:
                statistics.typename = typename
            return statistics
        except Exception as e:
            logger.error(f"Error getting statistics: {str(e)}")
            raise

    async def write_statistics(self, typename: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
                "partitions": self.statistics,
            }

            # Write the statistics to a json file
            output_file_name = f"{self.output_path}/statistics.json.ignore"
            with open(output_file_name, "w") as f:
                f.write(orjson.dumps(statistics).decode("utf-8"))

            destination_file_path = get_object_store_prefix(output_file_name)
            # Push the file to the object store
            await ObjectStore.upload_file(
                source=output_file_name,
                destination=destination_file_path,
            )

            if typename:
                statistics["typename"] = typename
            # Update aggregated statistics at run root in object store
            try:
                await self._update_run_aggregate(destination_file_path, statistics)
            except Exception as e:
                logger.warning(f"Failed to update aggregated statistics: {str(e)}")
            return statistics
        except Exception as e:
            logger.error(f"Error writing statistics: {str(e)}")

    async def _update_run_aggregate(
        self, per_path_destination: str, statistics: Dict[str, Any]
    ) -> None:
        """Aggregate stats into a single file at the workflow run root.

        Args:
            per_path_destination: Object store destination path for this stats file
                                 (used as key in the aggregate map)
            statistics: The statistics dictionary to store
        """
        logger.info(f"Starting _update_run_aggregate for phase: {self.phase} (value: {self.phase.value})")
        # Build the workflow run root path directly using utility functions (no path manipulation!)
        # build_output_path() returns: "artifacts/apps/{app}/workflows/{workflow_id}/{run_id}"
        # We need the local path: "./local/tmp/artifacts/apps/{app}/workflows/{workflow_id}/{run_id}"
        workflow_run_root_relative = build_output_path()
        output_file_name = f"{TEMPORARY_PATH}{workflow_run_root_relative}/statistics.json.ignore"
        destination_file_path = get_object_store_prefix(output_file_name)


        # Load existing aggregate from object store if present
        # New structure: {"Extract": [...], "Transform": [...], "Publish": [...]}
        aggregate_by_phase: Dict[str, List[Dict[str, Any]]] = {
            "Extract": [],
            "Transform": [],
            "Publish": []
        }
        
        try:
            # Download existing aggregate file if present
            await ObjectStore.download_file(
                source=destination_file_path,
                destination=output_file_name,
            )
            # Load existing JSON structure
            with open(output_file_name, "r") as f:
                existing_aggregate = orjson.loads(f.read())
                # Phase-based structure
                aggregate_by_phase.update(existing_aggregate)
                logger.info(f"Successfully loaded existing aggregates")
        except Exception:
            logger.info(
                "No existing aggregate found or failed to read. Initializing a new aggregate structure."
            )

        # Add this entry to the appropriate phase
        logger.info(f"Adding statistics to phase '{self.phase.value}'")
        aggregate_by_phase[self.phase.value].append(statistics)
        
        with open(output_file_name, "w") as f:
            f.write(orjson.dumps(aggregate_by_phase).decode("utf-8"))
            logger.info(f"Successfully updated aggregate with entries for phase '{self.phase.value}'")
        
        # Upload aggregate to object store
        await ObjectStore.upload_file(
            source=output_file_name,
            destination=destination_file_path,
        )
