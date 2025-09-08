import glob
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Optional, Union

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.error_codes import IOError
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd


class Input(ABC):
    """
    Abstract base class for input data sources.
    """

    async def download_files(self, file_extension: str) -> List[str]:
        """Download files from object store if not available locally.

        Flow:
        1. Check if files exist locally at self.path
        2. If not, try to download from object store
        3. Filter by self.file_names if provided
        4. Return list of file paths for logging purposes

        Args:
            file_extension (str): File extension to search for (e.g., '.parquet', '.json')

        Returns:
            List[str]: List of file paths (for logging/counting purposes)

        Raises:
            AttributeError: When the input class doesn't support file operations
            IOError: When no files found locally or in object store
        """
        # Check if this input class supports file operations
        if not hasattr(self, "path") or not self.path:
            raise AttributeError(
                f"{self.__class__.__name__} does not support file operations. "
                f"This method is only available for file-based inputs like ParquetInput and JsonInput."
            )

        def _find_files(search_path: Optional[str] = None) -> List[str]:
            """Find files at the specified path, optionally filtering by self.file_names.

            Args:
                search_path: Path to search in. If None, uses self.path.
            """
            path_to_search = search_path if search_path is not None else self.path

            if os.path.isfile(path_to_search) and path_to_search.endswith(
                file_extension
            ):
                # Single file - check if it matches target files (if specified)
                if hasattr(self, "file_names") and self.file_names:
                    file_name = os.path.basename(path_to_search)
                    if not any(
                        path_to_search.endswith(target) or file_name == target
                        for target in self.file_names
                    ):
                        return []
                return [path_to_search]

            elif os.path.isdir(path_to_search):
                # Directory - find all files in directory
                all_files = glob.glob(
                    os.path.join(path_to_search, "**", f"*{file_extension}"),
                    recursive=True,
                )

                # Filter by file names if specified
                if hasattr(self, "file_names") and self.file_names:
                    filtered_files = []
                    for file_name in self.file_names:
                        # Support both relative and absolute file names
                        matching_files = [
                            f
                            for f in filter(
                                lambda f: f.endswith(file_name)
                                or os.path.basename(f) == file_name,
                                all_files,
                            )
                        ]
                        filtered_files.extend(matching_files)
                    return filtered_files
                else:
                    return all_files

            return []

        # Step 1: Check if files exist locally
        local_files = _find_files()
        if local_files:
            logger.info(
                f"Found {len(local_files)} {file_extension} files locally at: {self.path}"
            )
            return local_files

        # Step 2: Try to download from object store
        logger.info(
            f"No local {file_extension} files found at {self.path}, checking object store..."
        )

        try:
            # Determine what to download based on path type and filters
            if self.path.endswith(file_extension):
                # Single file case (file_names validation already ensures this is valid)
                source_path = get_object_store_prefix(self.path)
                await ObjectStore.download_file(source=source_path)

            elif hasattr(self, "file_names") and self.file_names:
                # Directory with specific files - download each file individually
                for file_name in self.file_names:
                    file_path = os.path.join(self.path, file_name)
                    source_path = get_object_store_prefix(file_path)
                    await ObjectStore.download_file(source=source_path)
            else:
                # Download entire directory
                source_path = get_object_store_prefix(self.path)
                await ObjectStore.download_prefix(source=source_path)

            # After all downloads, search for files in the downloaded location
            base_source_path = get_object_store_prefix(self.path)
            # Ensure the source path is relative for proper joining with TEMPORARY_PATH
            # This prevents os.path.join from ignoring TEMPORARY_PATH if source_path is absolute
            relative_source_path = (
                base_source_path[1:]
                if base_source_path.startswith("/")
                else base_source_path
            )
            downloaded_path = os.path.join(TEMPORARY_PATH, relative_source_path)
            downloaded_files = _find_files(downloaded_path)

            # Check results
            if downloaded_files:
                logger.info(
                    f"Successfully downloaded {len(downloaded_files)} {file_extension} files from object store"
                )
                return downloaded_files
            else:
                raise IOError(
                    f"{IOError.OBJECT_STORE_READ_ERROR}: Downloaded from object store but no {file_extension} files found"
                )

        except Exception as e:
            logger.error(f"Failed to download from object store: {str(e)}")
            raise IOError(
                f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: No {file_extension} files found locally at '{self.path}' and failed to download from object store. "
                f"Error: {str(e)}"
            )

    @abstractmethod
    async def get_batched_dataframe(
        self,
    ) -> Union[Iterator["pd.DataFrame"], AsyncIterator["pd.DataFrame"]]:
        """
        Get an iterator of batched pandas DataFrames.

        Returns:
            Iterator["pd.DataFrame"]: An iterator of batched pandas DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Get a single pandas DataFrame.

        Returns:
            "pd.DataFrame": A pandas DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_batched_daft_dataframe(
        self,
    ) -> Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:  # noqa: F821
        """
        Get an iterator of batched daft DataFrames.

        Returns:
            Iterator[daft.DataFrame]: An iterator of batched daft DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Get a single daft DataFrame.

        Returns:
            daft.DataFrame: A daft DataFrame.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError
