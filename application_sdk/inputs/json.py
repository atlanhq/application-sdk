import os
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Optional,
    Union,
)

import pandas as pd

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.config import get_settings
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft


class AsyncDataFrameIterator(AsyncIterator[pd.DataFrame]):
    """Adapter class to convert an async generator to an AsyncIterator"""

    def __init__(self, async_gen: AsyncGenerator[pd.DataFrame, None]):
        self.async_gen = async_gen

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.async_gen.__anext__()
        except StopAsyncIteration:
            raise StopAsyncIteration


class AsyncDaftDataFrameIterator(AsyncIterator["daft.DataFrame"]):  # noqa: F821
    """Adapter class to convert an async generator to an AsyncIterator for daft.DataFrame"""

    def __init__(self, async_gen: AsyncGenerator["daft.DataFrame", None]):  # noqa: F821
        self.async_gen = async_gen

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.async_gen.__anext__()
        except StopAsyncIteration:
            raise StopAsyncIteration


class JsonInput(Input):
    path: str
    chunk_size: Optional[int]
    file_names: Optional[List[str]]
    download_file_prefix: Optional[str]

    def __init__(
        self,
        path: str,
        file_names: Optional[List[str]] = None,
        download_file_prefix: Optional[str] = None,
        chunk_size: Optional[int] = None,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): The path to the input directory.
            file_names (Optional[List[str]]): The list of files to read.
            download_file_prefix (Optional[str]): The prefix path in object store.
            chunk_size (Optional[int]): The chunk size to read the data. If None, uses config value.
            **kwargs (Dict[str, Any]): Keyword arguments for initialization.
        """
        self.path = path
        settings = get_settings()
        self.chunk_size = chunk_size or settings.chunk_size
        self.file_names = file_names
        self.download_file_prefix = download_file_prefix

    async def download_files(self):
        """Download the files from the object store to the local path"""
        if not self.file_names:
            logger.debug("No files to download")
            return

        for file_name in self.file_names or []:
            try:
                # Ensure path is not None before joining
                local_path = os.path.join(self.path or "", file_name)
                if not os.path.exists(local_path):
                    # Ensure download_file_prefix is not None before joining
                    prefix = self.download_file_prefix or ""
                    remote_path = os.path.join(prefix, file_name)
                    ObjectStoreInput.download_file_from_object_store(
                        remote_path,
                        local_path,
                    )
            except Exception as e:
                logger.error(f"Error downloading file {file_name}: {str(e)}")
                raise e

    @classmethod
    def re_init(cls, **kwargs: Any):
        """Re-initialize the input class with given keyword arguments.

        Args:
            **kwargs (Any): Keyword arguments for re-initialization.

        Returns:
            JsonInput: A new instance of the JsonInput class
        """
        # Extract path parameter, defaulting to empty string if missing
        path = kwargs.get("path", "")
        if not isinstance(path, str):
            path = str(path) if path is not None else ""

        # Get output path to construct the full path
        output_path = kwargs.get("output_path", "")
        if not isinstance(output_path, str):
            output_path = str(output_path) if output_path is not None else ""

        # Combine paths properly for the full path
        full_path = f"{output_path}{path}"

        # Get the output prefix for downloads
        output_prefix = kwargs.get("output_prefix", "")
        if not isinstance(output_prefix, str):
            output_prefix = str(output_prefix) if output_prefix is not None else ""

        # Extract and clean up the file_names input
        file_names_input = kwargs.get("file_names")

        # Handle file_names correctly
        file_names = None

        if file_names_input is not None:
            # Convert to a list of strings
            if isinstance(file_names_input, list):
                # Clean list by converting all items to strings and filtering None
                file_names = []  # Start with empty list
                for item in file_names_input:
                    if item is not None:
                        # Ensure each item is a string
                        file_names.append(str(item))
            elif isinstance(file_names_input, str):
                # Single string case - wrap in a list
                file_names = [file_names_input]
            else:
                # Try to convert a single item to string
                try:
                    file_names = [str(file_names_input)]
                except (ValueError, TypeError):
                    file_names = None

        # Handle chunk_size conversion
        chunk_size = None
        chunk_size_raw = kwargs.get("chunk_size")
        if chunk_size_raw is not None:
            if isinstance(chunk_size_raw, int):
                chunk_size = chunk_size_raw
            else:
                try:
                    # Convert to int if it's a string or other numeric type
                    if isinstance(chunk_size_raw, (str, float)):
                        chunk_size = int(chunk_size_raw)
                except (ValueError, TypeError):
                    pass  # Keep as None if conversion fails

        # Create a new instance with properly typed parameters
        return cls(
            path=full_path,
            file_names=file_names,
            download_file_prefix=output_prefix,
            chunk_size=chunk_size,
        )

    def get_batched_dataframe(
        self,
    ) -> Union[Iterator[pd.DataFrame], AsyncIterator[pd.DataFrame]]:
        """Get an iterator of batched pandas DataFrames.

        Returns:
            Union[Iterator[pd.DataFrame], AsyncIterator[pd.DataFrame]]:
                An iterator of batched pandas DataFrames.
        """

        # Create a helper function to get batched dataframes
        async def _get_batched_dataframe_helper() -> AsyncGenerator[pd.DataFrame, None]:
            try:
                await self.download_files()
                for file_name in self.file_names or []:
                    local_path = os.path.join(self.path or "", file_name)
                    json_reader_obj = pd.read_json(
                        local_path,
                        chunksize=self.chunk_size,
                        lines=True,
                    )
                    for chunk in json_reader_obj:
                        yield chunk
            except Exception as e:
                logger.error(f"Error reading batched data from JSON: {str(e)}")

        # Return an AsyncIterator that wraps the async generator
        return AsyncDataFrameIterator(_get_batched_dataframe_helper())

    async def get_dataframe(self) -> pd.DataFrame:
        """Get a single pandas DataFrame.

        Returns:
            pd.DataFrame: A pandas DataFrame.
        """
        try:
            dataframes = []
            await self.download_files()
            for file_name in self.file_names or []:
                dataframes.append(
                    pd.read_json(
                        os.path.join(self.path, file_name),
                        lines=True,
                    )
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")
            # Return empty dataframe on error
            return pd.DataFrame()

    def get_batched_daft_dataframe(
        self,
    ) -> Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:  # noqa: F821
        """Get an iterator of batched daft DataFrames.

        Returns:
            Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:
                An iterator of batched daft DataFrames.
        """

        # Create a helper function to get batched daft dataframes
        async def _get_batched_daft_dataframe_helper() -> (
            AsyncGenerator["daft.DataFrame", None]
        ):  # noqa: F821
            try:
                import daft

                await self.download_files()
                for file_name in self.file_names or []:
                    local_path = os.path.join(self.path or "", file_name)
                    json_reader_obj = daft.read_json(
                        path=local_path,
                        _chunk_size=self.chunk_size,
                    )
                    yield json_reader_obj
            except Exception as e:
                logger.error(f"Error reading batched data from JSON: {str(e)}")

        # Return an AsyncIterator that wraps the async generator
        return AsyncDaftDataFrameIterator(_get_batched_daft_dataframe_helper())

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """Get a single daft DataFrame.

        Returns:
            daft.DataFrame: A daft DataFrame.
        """
        try:
            import daft

            dataframe_concat = None
            await self.download_files()
            for file_name in self.file_names or []:
                json_dataframe = daft.read_json(path=os.path.join(self.path, file_name))
                dataframe_concat = (
                    json_dataframe
                    if dataframe_concat is None
                    else dataframe_concat.concat(json_dataframe)
                )
            return (
                dataframe_concat if dataframe_concat is not None else daft.DataFrame()
            )
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
            # Try to return an empty daft DataFrame
            try:
                import daft

                return daft.DataFrame()
            except ImportError:
                raise e
