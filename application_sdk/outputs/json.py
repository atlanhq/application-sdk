import logging
import os
from typing import Any, Callable, Dict, Iterator, List, Optional

import daft
import pandas as pd
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.activities.common.models import MetadataModel
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.outputs import Output, is_empty_dataframe

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


def path_gen(chunk_start: int | None, chunk_count: int) -> str:
    """Generate a file path for a chunk.

    Args:
        chunk_start (int | None): Starting index of the chunk, or None for single chunk.
        chunk_count (int): Total number of chunks.

    Returns:
        str: Generated file path for the chunk.
    """
    if chunk_start is None:
        return f"{str(chunk_count)}.json"
    else:
        return f"{str(chunk_start+1)}-{str(chunk_count)}.json"


class JsonOutput(Output):
    """Output handler for writing data to JSON files.

    This class handles writing DataFrames to JSON files with support for chunking
    and automatic uploading to object store.

    Attributes:
        output_path (str): Path where JSON files will be written.
        upload_file_prefix (str): Prefix for files when uploading to object store.
        chunk_start (Optional[int]): Starting index for chunk numbering.
        buffer_size (int): Size of the buffer in bytes.
        chunk_size (int): Maximum number of records per chunk.
        total_record_count (int): Total number of records processed.
        chunk_count (int): Number of chunks created.
        path_gen (Callable): Function to generate file paths for chunks.
        buffer (List[pd.DataFrame]): Buffer holding DataFrames before writing.
        current_buffer_size (int): Current size of the buffer.
    """

    def __init__(
        self,
        output_suffix: str,
        output_path: Optional[str] = None,
        output_prefix: Optional[str] = None,
        typename: Optional[str] = None,
        state: Optional[ActivitiesState] = None,
        chunk_start: Optional[int] = None,
        buffer_size: int = 1024 * 1024 * 10,
        chunk_size: int = 100,
        total_record_count: int = 0,
        chunk_count: int = 0,
        path_gen: Callable[[int | None, int], str] = path_gen,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JSON output handler.

        Args:
            output_path (str): Path where JSON files will be written.
            upload_file_prefix (str): Prefix for files when uploading to object store.
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            buffer_size (int, optional): Size of the buffer in bytes.
                Defaults to 10MB (1024 * 1024 * 10).
            chunk_size (int, optional): Maximum number of records per chunk.
                Defaults to 100000.
            total_record_count (int, optional): Initial total record count.
                Defaults to 0.
            chunk_count (int, optional): Initial chunk count.
                Defaults to 0.
            path_gen (Callable, optional): Function to generate file paths.
                Defaults to path_gen function.
        """
        self.output_path = output_path
        self.output_suffix = output_suffix
        self.output_prefix = output_prefix
        self.typename = typename
        self.chunk_start = chunk_start
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.buffer_size = buffer_size
        self.chunk_size = chunk_size
        self.buffer: List[pd.DataFrame] = []
        self.current_buffer_size = 0
        self.path_gen = path_gen
        self.state = state

    def re_init(
        self,
        output_path: str,
        typename: Optional[str] = None,
        chunk_count: int = 0,
        total_record_count: int = 0,
        chunk_start: Optional[int] = None,
        **kwargs: Dict[str, Any],
    ):
        self.total_record_count = 0
        self.chunk_count = 0
        self.chunk_start = None
        self.output_path = output_path
        self.output_path = f"{self.output_path}{self.output_suffix}"
        if typename:
            self.typename = typename
            self.output_path = f"{self.output_path}/{self.typename}"
        if chunk_count:
            self.chunk_count = chunk_count
        if total_record_count:
            self.total_record_count = total_record_count
        if chunk_start is not None:
            self.chunk_start = chunk_start
        os.makedirs(f"{self.output_path}", exist_ok=True)

    async def write_batched_df(self, batched_df: Iterator[pd.DataFrame]):
        """Write a batched pandas DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            df (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            for df in batched_df:
                if not is_empty_dataframe(df):
                    await self.write_df(df)
        except Exception as e:
            activity.logger.error(f"Error writing batched dataframe to json: {str(e)}")

    async def write_df(self, df: pd.DataFrame):
        """Write a pandas DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            df (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        if len(df) == 0:
            return

        try:
            # Split the DataFrame into chunks
            partition = (
                self.chunk_size
                if self.chunk_start is None
                else min(self.chunk_size, self.buffer_size)
            )
            chunks = [df[i : i + partition] for i in range(0, len(df), partition)]

            for chunk in chunks:
                self.buffer.append(chunk)
                self.current_buffer_size += len(chunk)

                if self.current_buffer_size >= partition:
                    await self._flush_buffer()

            await self._flush_buffer()

        except Exception as e:
            activity.logger.error(f"Error writing dataframe to json: {str(e)}")

    async def write_batched_daft_df(self, batched_df: Iterator[daft.DataFrame]):
        """Write a batched daft DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            df (daft.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        try:
            for df in batched_df:
                if not is_empty_dataframe(df):
                    await self.write_daft_df(df)
        except Exception as e:
            activity.logger.error(
                f"Error writing batched daft dataframe to json: {str(e)}"
            )

    async def write_daft_df(self, df: daft.DataFrame):
        """Write a daft DataFrame to JSON files.

        This method converts the daft DataFrame to pandas and writes it to JSON files.

        Args:
            df (daft.DataFrame): The DataFrame to write.

        Note:
            Daft does not have built-in JSON writing support, so we convert to pandas.
        """
        # Daft does not have a built in method to write the daft dataframe to json
        # So we convert it to pandas dataframe and write it to json
        await self.write_df(df.to_pandas())

    async def _flush_buffer(self):
        """Flush the current buffer to a JSON file.

        This method combines all DataFrames in the buffer, writes them to a JSON file,
        and uploads the file to the object store.

        Note:
            If the buffer is empty or has no records, the method returns without writing.
        """
        if not self.buffer or not self.current_buffer_size:
            return
        combined_df = pd.concat(self.buffer)

        # Write DataFrame to JSON file
        if not combined_df.empty:
            self.chunk_count += 1
            self.total_record_count += len(combined_df)
            output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count)}"
            combined_df.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.output_prefix, output_file_name
            )

        self.buffer.clear()
        self.current_buffer_size = 0

    def get_metadata(self, typename: Optional[str] = None) -> MetadataModel:
        """Get metadata about the output.

        This method returns a MetadataModel object with total record count and chunk count.

        Args:
            typename (str): Type name of the entity e.g database, schema, table.
        """
        return MetadataModel(
            total_record_count=self.total_record_count,
            chunk_count=self.chunk_count,
            typename=typename,
        )
