from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Optional, Union

from application_sdk.inputs import Input
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    import daft
    import pandas as pd

logger = get_logger(__name__)


class JsonInput(Input):
    """
    JSON Input class to read data from JSON files using daft and pandas.
    Supports reading both single files and directories containing multiple JSON files.
    """

    _EXTENSION = ".json"

    def __init__(
        self,
        path: str,
        file_names: Optional[List[str]] = None,
        chunk_size: int = 100000,
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): Path to JSON file or directory containing JSON files.
                It accepts both types of paths:
                local path or object store path
                Wildcards are not supported.
            file_names (Optional[List[str]]): List of specific file names to read. Defaults to None.
            chunk_size (int): Number of rows per batch. Defaults to 100000.

        Raises:
            ValueError: When path is not provided or when single file path is combined with file_names
        """

        # Validate that single file path and file_names are not both specified
        if path.endswith(self._EXTENSION) and file_names:
            raise ValueError(
                f"Cannot specify both a single file path ('{path}') and file_names filter. "
                f"Either provide a directory path with file_names, or specify the exact file path without file_names."
            )

        self.path = path
        self.chunk_size = chunk_size
        self.file_names = file_names

    async def get_batched_dataframe(
        self,
    ) -> Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            json_files = await self.download_files()
            logger.info(f"Reading {len(json_files)} JSON files in batches")

            for json_file in json_files:
                json_reader_obj = pd.read_json(
                    json_file,
                    chunksize=self.chunk_size,
                    lines=True,
                )
                for chunk in json_reader_obj:
                    yield chunk
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")
            raise

    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            json_files = await self.download_files()
            logger.info(f"Reading {len(json_files)} JSON files as pandas dataframe")

            return pd.concat(
                (pd.read_json(json_file, lines=True) for json_file in json_files),
                ignore_index=True,
            )

        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")
            raise

    async def get_batched_daft_dataframe(
        self,
    ) -> Union[AsyncIterator["daft.DataFrame"], Iterator["daft.DataFrame"]]:  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a batched daft dataframe
        """
        try:
            import daft

            # Ensure files are available (local or downloaded)
            json_files = await self.download_files()
            logger.info(f"Reading {len(json_files)} JSON files as daft batches")

            # Yield each discovered file as separate batch with chunking
            for json_file in json_files:
                yield daft.read_json(json_file, _chunk_size=self.chunk_size)
        except Exception as e:
            logger.error(f"Error reading batched data from JSON using daft: {str(e)}")
            raise

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        try:
            import daft

            # Ensure files are available (local or downloaded)
            json_files = await self.download_files()
            logger.info(f"Reading {len(json_files)} JSON files with daft")

            # Use the discovered/downloaded files directly
            return daft.read_json(json_files)
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
            raise
