import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Union

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.error_codes import IOError
from application_sdk.common.utils import find_files_by_extension
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

    async def download_files(self) -> List[str]:
        """Download files from object store if not available locally.

        Flow:
        1. Check if files exist locally at self.path
        2. If not, try to download from object store
        3. Filter by self.file_names if provided
        4. Return list of file paths for logging purposes

        Returns:
            List[str]: List of file paths (for logging/counting purposes)

        Raises:
            AttributeError: When the input class doesn't support file operations or _extension
            IOError: When no files found locally or in object store
        """
        # Check if this input class supports file operations
        if not hasattr(self, "path") or not self.path:
            raise AttributeError(
                f"{self.__class__.__name__} does not support file operations. "
                f"This method is only available for file-based inputs like ParquetInput and JsonInput."
            )

        # Check if extension is defined
        if not hasattr(self, "_extension"):
            raise AttributeError(
                f"{self.__class__.__name__} must define _extension attribute. "
                f"This should be set in the constructor (e.g., self._extension = '.parquet')."
            )

        # Step 1: Check if files exist locally
        local_files = find_files_by_extension(
            self.path, self._extension, getattr(self, "file_names", None)
        )
        if local_files:
            logger.info(
                f"Found {len(local_files)} {self._extension} files locally at: {self.path}"
            )
            return local_files

        # Step 2: Try to download from object store
        logger.info(
            f"No local {self._extension} files found at {self.path}, checking object store..."
        )

        try:
            # Determine what to download based on path type and filters
            downloaded_source_paths = []

            if self.path.endswith(self._extension):
                # Single file case (file_names validation already ensures this is valid)
                source_path = get_object_store_prefix(self.path)
                await ObjectStore.download_file(source=source_path)
                downloaded_source_paths.append(source_path)

            elif hasattr(self, "file_names") and self.file_names:
                # Directory with specific files - download each file individually
                for file_name in self.file_names:
                    file_path = os.path.join(self.path, file_name)
                    source_path = get_object_store_prefix(file_path)
                    await ObjectStore.download_file(source=source_path)
                    downloaded_source_paths.append(source_path)
            else:
                # Download entire directory
                source_path = get_object_store_prefix(self.path)
                await ObjectStore.download_prefix(source=source_path)
                downloaded_source_paths.append(source_path)

            # After all downloads, search for files using the exact paths we downloaded
            downloaded_files = []
            for source_path in downloaded_source_paths:
                downloaded_path = os.path.join(TEMPORARY_PATH, source_path)
                if os.path.isfile(downloaded_path):
                    # Single file
                    downloaded_files.append(downloaded_path)
                elif os.path.isdir(downloaded_path):
                    # Directory - find all files with extension
                    found_files = find_files_by_extension(
                        downloaded_path, self._extension
                    )
                    downloaded_files.extend(found_files)

            # Check results
            if downloaded_files:
                logger.info(
                    f"Successfully downloaded {len(downloaded_files)} {self._extension} files from object store"
                )
                return downloaded_files
            else:
                raise IOError(
                    f"{IOError.OBJECT_STORE_READ_ERROR}: Downloaded from object store but no {self._extension} files found"
                )

        except Exception as e:
            logger.error(f"Failed to download from object store: {str(e)}")
            raise IOError(
                f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: No {self._extension} files found locally at '{self.path}' and failed to download from object store. "
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
