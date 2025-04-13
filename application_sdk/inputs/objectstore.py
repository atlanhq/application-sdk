"""Object store interface for the application."""

import os
from typing import List

import orjson
from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger

activity.logger = get_logger(__name__)


class ObjectStoreInput:
    OBJECT_STORE_NAME = os.getenv("OBJECT_STORE_NAME", "objectstore")
    OBJECT_GET_OPERATION = "get"
    OBJECT_LIST_OPERATION = "list"

    @classmethod
    def list_files_from_object_store(
        cls,
        relative_path: str = "",
    ) -> List[str]:
        """
        Lists all files in the object store for a given prefix path.

        Args:
            relative_path (str): The relative path from the prefix to list. Defaults to empty string.

        Returns:
            list: A list of file paths found in the object store.

        Raises:
            Exception: If there's an error listing files from the object store.
        """
        try:
            with DaprClient() as client:
                metadata = {"fileName": relative_path}
                try:
                    # Invoke the object store binding with list operation
                    response = client.invoke_binding(
                        binding_name=cls.OBJECT_STORE_NAME,
                        operation=cls.OBJECT_LIST_OPERATION,
                        binding_metadata=metadata,
                    )
                    file_list = orjson.loads(response.data.decode("utf-8"))
                    activity.logger.debug(
                        f"Successfully listed {len(file_list)} files from: {relative_path}"
                    )
                    return file_list
                except Exception as e:
                    activity.logger.error(
                        f"Error listing files in object store path {relative_path}: {str(e)}"
                    )
                    raise e
        except Exception as e:
            activity.logger.error(
                f"Error connecting to object store: {str(e)}"
            )
            raise e

    @classmethod
    def download_files_from_object_store(
        cls,
        download_file_prefix: str,
        file_path: str,
    ) -> None:
        """
        Downloads all files from the object store for a given prefix.

        Args:
            download_file_prefix (str): The base path in the object store to download files from.
            local_directory (str): The local directory where the files should be downloaded.

        Raises:
            Exception: If there's an error downloading any file from the object store.
        """
        try:
            # Calculate the relative path
            relative_path = os.path.relpath(file_path, download_file_prefix)
            
            # List all files in the object store path using the new function
            file_list = cls.list_files_from_object_store(relative_path)

            if not file_list:
                activity.logger.info(
                    f"No files found in object store path: {download_file_prefix}/{relative_path}"
                )
                return

            # Download each file
            for relative_path in file_list:
                local_file_path = os.path.join(
                    file_path, os.path.basename(relative_path)
                )
                cls.download_file_from_object_store(
                    download_file_prefix, local_file_path
                )

            activity.logger.debug(
                f"Successfully downloaded all files from: {download_file_prefix}"
            )
        except Exception as e:
            activity.logger.error(
                f"Error downloading files from object store: {str(e)}"
            )
            raise e

    @classmethod
    def download_file_from_object_store(
        cls,
        download_file_prefix: str,
        file_path: str,
    ) -> None:
        """Downloads a single file from the object store.

        Args:
            download_file_prefix (str): The base path to calculate relative paths from.
            file_path (str): The full path to where the file should be downloaded.

        Raises:
            Exception: If there's an error downloading the file from the object store.
        """
        with DaprClient() as client:
            relative_path = os.path.relpath(file_path, download_file_prefix)
            metadata = {"key": relative_path, "fileName": relative_path}

            try:
                response = client.invoke_binding(
                    binding_name=cls.OBJECT_STORE_NAME,
                    operation=cls.OBJECT_GET_OPERATION,
                    binding_metadata=metadata,
                )
                # check if response.data is in binary format
                write_mode = "wb" if isinstance(response.data, bytes) else "w"
                with open(file_path, write_mode) as f:
                    f.write(response.data)
                    f.close()

                activity.logger.debug(f"Successfully downloaded file: {relative_path}")
            except Exception as e:
                activity.logger.error(
                    f"Error downloading file {relative_path} to object store: {str(e)}"
                )
                raise e
