import os
from typing import Any, Callable, Dict, List, Optional, Union

import orjson
from temporalio import activity

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.config import get_settings
from application_sdk.outputs import Output
from application_sdk.outputs.objectstore import ObjectStoreOutput

activity.logger = get_logger(__name__)


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
        output_suffix (str): Suffix for files when uploading to object store.
        output_path (str): Path where JSON files will be written.
        output_prefix (str): Prefix for files when uploading to object store.
        typename (Optional[str]): Type name of the entity e.g database, schema, table.
        state (Optional[ActivitiesState]): State object for tracking activity statistics.
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
        buffer_size: int = 100000,
        chunk_size: Optional[int] = None,
        total_record_count: int = 0,
        chunk_count: int = 0,
        path_gen: Callable[[int | None, int], str] = path_gen,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JSON output handler.

        Args:
            output_path (str): Path where JSON files will be written.
            output_suffix (str): Prefix for files when uploading to object store.
            output_prefix (Optional[str], optional): Prefix for files where the files will be written and uploaded.
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            buffer_size (int, optional): Size of the buffer in bytes.
                Defaults to 10MB (1024 * 1024 * 10).
            chunk_size (Optional[int], optional): Maximum number of records per chunk. If None, uses config value.
                Defaults to None.
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
        settings = get_settings()
        self.chunk_size = chunk_size or settings.chunk_size
        self.buffer: List[Union["pd.DataFrame", "daft.DataFrame"]] = []  # noqa: F821
        self.current_buffer_size = 0
        self.path_gen = path_gen
        self.state = state

    @classmethod
    def re_init(
        cls,
        output_path: str,
        typename: Optional[str] = None,
        chunk_count: int = 0,
        total_record_count: int = 0,
        chunk_start: Optional[int] = None,
        output_suffix: str = None,
        **kwargs: Dict[str, Any],
    ):
        """Re-initialize the output class with given keyword arguments.

        Args:
            output_path (str): Path where JSON files will be written.
            typename (str, optional): Type name of the entity e.g database, schema, table.
                Defaults to None.
            chunk_count (int, optional): Initial chunk count.
                Defaults to 0.
            total_record_count (int, optional): Initial total record count.
                Defaults to 0.
            chunk_start (Optional[int], optional): Starting index for chunk numbering.
                Defaults to None.
            output_suffix (str, optional): Suffix for output files.
                Defaults to None.
            kwargs (Dict[str, Any]): Additional keyword arguments.
        """
        output_path = f"{output_path}{output_suffix}"
        if typename:
            output_path = f"{output_path}/{typename}"
        os.makedirs(f"{output_path}", exist_ok=True)

        # For Query Extraction
        start_marker = kwargs.get("start_marker")
        end_marker = kwargs.get("end_marker")
        if start_marker and end_marker:
            kwargs["path_gen"] = (
                lambda chunk_start, chunk_count: f"{start_marker}_{end_marker}.json"
            )
        return cls(
            output_suffix=output_suffix,
            output_path=output_path,
            typename=typename,
            chunk_count=chunk_count,
            total_record_count=total_record_count,
            chunk_start=chunk_start,
            **kwargs,
        )

    async def write_dataframe(self, dataframe: "pd.DataFrame"):
        """Write a pandas DataFrame to JSON files.

        This method writes the DataFrame to JSON files, potentially splitting it
        into chunks based on chunk_size and buffer_size settings.

        Args:
            dataframe (pd.DataFrame): The DataFrame to write.

        Note:
            If the DataFrame is empty, the method returns without writing.
        """
        if len(dataframe) == 0:
            return

        try:
            # Split the DataFrame into chunks
            partition = (
                self.chunk_size
                if self.chunk_start is None
                else min(self.chunk_size, self.buffer_size)
            )
            chunks = [
                dataframe[i : i + partition]
                for i in range(0, len(dataframe), partition)
            ]

            for chunk in chunks:
                self.buffer.append(chunk)
                self.current_buffer_size += len(chunk)

                if self.current_buffer_size >= partition:
                    await self._flush_buffer()

            await self._flush_buffer()

        except Exception as e:
            activity.logger.error(f"Error writing dataframe to json: {str(e)}")

    async def write_daft_dataframe(self, dataframe: "daft.DataFrame"):  # noqa: F821
        """Write a daft DataFrame to JSON files.

        This method converts the daft DataFrame to pandas and writes it to JSON files.

        Args:
            dataframe (daft.DataFrame): The DataFrame to write.

        Note:
            Daft does not have built-in JSON writing support, so we are using orjson.
        """
        # Daft does not have a built in method to write the daft dataframe to json
        # So we are using orjson to write the data to json in a more memory efficient way
        buffer = []

        for row in dataframe:
            self.total_record_count += 1
            # Serialize the row and add it to the buffer
            buffer.append(
                orjson.dumps(row, option=orjson.OPT_APPEND_NEWLINE).decode("utf-8")
            )

            # If the buffer reaches the specified size, write it to the file
            if self.chunk_size and len(buffer) >= self.chunk_size:
                self.chunk_count += 1
                output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count)}"
                with open(output_file_name, "w") as f:
                    f.writelines(buffer)
                buffer.clear()  # Clear the buffer

        # Write any remaining rows in the buffer
        if buffer:
            self.chunk_count += 1
            output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count)}"
            with open(output_file_name, "w") as f:
                f.writelines(buffer)
            buffer.clear()

        # Push the file to the object store
        await ObjectStoreOutput.push_files_to_object_store(
            self.output_prefix, self.output_path
        )

    async def _flush_buffer(self):
        """Flush the current buffer to a JSON file.

        This method combines all DataFrames in the buffer, writes them to a JSON file,
        and uploads the file to the object store.

        Note:
            If the buffer is empty or has no records, the method returns without writing.
        """
        if not self.buffer or not self.current_buffer_size:
            return
        combined_dataframe = pd.concat(self.buffer)

        # Write DataFrame to JSON file
        if not combined_dataframe.empty:
            self.chunk_count += 1
            self.total_record_count += len(combined_dataframe)
            output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count)}"
            combined_dataframe.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                self.output_prefix, output_file_name
            )

        self.buffer.clear()
        self.current_buffer_size = 0
