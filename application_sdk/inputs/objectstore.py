"""Object store interface for the application."""

import json
import os
from typing import Dict, List, Optional

import orjson
from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.constants import OBJECT_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class ObjectStoreInput:
    OBJECT_GET_OPERATION = "get"
    OBJECT_LIST_OPERATION = "list"

    @classmethod
    def _invoke_dapr_binding(
        cls, operation: str, metadata: Dict[str, str], data: Optional[bytes] = None
    ) -> bytes:
        """
        Common method to invoke Dapr binding operations.

        Args:
            operation (str): The Dapr binding operation to perform
            metadata (Dict[str, str]): Metadata for the binding operation
            data (Optional[bytes]): Optional data to send with the request

        Returns:
            bytes: Response data from the Dapr binding

        Raises:
            Exception: If there's an error with the Dapr binding operation
        """
        try:
            with DaprClient() as client:
                response = client.invoke_binding(
                    binding_name=OBJECT_STORE_NAME,
                    operation=operation,
                    data=data,
                    binding_metadata=metadata,
                )
                return response.data
        except Exception as e:
            logger.error(f"Error in Dapr binding operation '{operation}': {str(e)}")
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
            # List all files in the object store path
            relative_path = os.path.relpath(file_path, download_file_prefix)
            metadata = {"fileName": relative_path}

            try:
                # Assuming the object store binding supports a "list" operation
                response_data = cls._invoke_dapr_binding(
                    operation=cls.OBJECT_LIST_OPERATION, metadata=metadata
                )
                file_list = orjson.loads(response_data.decode("utf-8"))
            except Exception as e:
                logger.error(
                    f"Error listing files in object store path {download_file_prefix}: {str(e)}"
                )
                raise e

            if not file_list:
                logger.info(
                    f"No files found in object store path: {download_file_prefix}"
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

            logger.info(
                f"Successfully downloaded all files from: {download_file_prefix}"
            )
        except Exception as e:
            logger.error(f"Error downloading files from object store: {str(e)}")
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
        relative_path = os.path.relpath(file_path, download_file_prefix)
        metadata = {"key": relative_path, "fileName": relative_path}

        try:
            response_data = cls._invoke_dapr_binding(
                operation=cls.OBJECT_GET_OPERATION, metadata=metadata
            )

            # check if response.data is in binary format
            write_mode = "wb" if isinstance(response_data, bytes) else "w"
            with open(file_path, write_mode) as f:
                f.write(response_data)

            logger.info(f"Successfully downloaded file: {relative_path}")
        except Exception as e:
            logger.error(
                f"Error downloading file {relative_path} to object store: {str(e)}"
            )
            raise e

    @classmethod
    def _extract_path_from_prefix(cls, absolute_path: str, prefix: str) -> str:
        """
        Extract the path starting from the given prefix.

        Args:
            absolute_path: Full path returned by Dapr list operation
            prefix: The prefix used in the list operation

        Returns:
            str: Path starting from the prefix

        Example:
            absolute_path = '/private/tmp/dapr/objectstore/aac273c6-8728-4a4a-953f-94cbef28bf04/0197ab76-60e3-7ac1-86bf-98e3831a808e/raw/column/statistics.json.ignore'
            prefix = 'aac273c6-8728-4a4a-953f-94cbef28bf04/0197ab76-60e3-7ac1-86bf-98e3831a808e'
            returns = 'aac273c6-8728-4a4a-953f-94cbef28bf04/0197ab76-60e3-7ac1-86bf-98e3831a808e/raw/column/statistics.json.ignore'
        """
        # Find where the prefix starts in the absolute path
        prefix_index = absolute_path.find(prefix)
        if prefix_index != -1:
            return absolute_path[prefix_index:]

        return os.path.basename(absolute_path)

    @classmethod
    def list_all_files(cls, prefix: str = "") -> List[str]:
        """
        List all files in the object store under a given prefix.

        Args:
            prefix (str): The prefix to filter files. Empty string returns all files.

        Returns:
            List[str]: List of file paths in the object store

        Raises:
            Exception: If there's an error listing files from the object store.
        """
        try:
            # Prepare metadata for listing
            metadata = {}
            if prefix:
                metadata["prefix"] = prefix
                # Also try fileName as some bindings might use this
                metadata["fileName"] = prefix

            data = json.dumps({"prefix": prefix}).encode("utf-8") if prefix else None
            response_data = cls._invoke_dapr_binding(
                operation=cls.OBJECT_LIST_OPERATION, metadata=metadata, data=data
            )

            if not response_data:
                logger.info(f"No files found in object store with prefix: {prefix}")
                return []

            decoded_data = response_data.decode("utf-8")
            if not decoded_data.strip():
                logger.info(f"Empty response from object store for prefix: {prefix}")
                return []

            file_list = orjson.loads(decoded_data)

            # Log what we actually received
            logger.info(
                f"Object store response type: {type(file_list)}, content: {file_list}"
            )

            if not isinstance(file_list, list):
                logger.error(f"Expected list from object store, got {type(file_list)}")
                # Try to handle dict response - some object stores return dict with different keys
                if isinstance(file_list, dict):
                    if "files" in file_list:
                        file_list = file_list["files"]
                        logger.info(f"Extracted files from dict response: {file_list}")
                    elif "keys" in file_list:
                        file_list = file_list["keys"]
                        logger.info(f"Extracted keys from dict response: {file_list}")
                    elif "Contents" in file_list:
                        # S3-style response with Contents array of objects
                        contents = file_list["Contents"]
                        if isinstance(contents, list):
                            # Extract the 'Key' field from each object in the Contents array
                            file_list = [
                                item.get("Key", "")
                                for item in contents
                                if isinstance(item, dict) and "Key" in item
                            ]
                            logger.info(
                                f"Extracted keys from S3 Contents response: {file_list}"
                            )
                        else:
                            logger.error(f"Contents is not a list: {contents}")
                            return []
                    else:
                        logger.error(
                            f"Dict response doesn't contain 'files', 'keys', or 'Contents': {file_list}"
                        )
                        logger.error(
                            f"Available keys in response: {list(file_list.keys()) if isinstance(file_list, dict) else 'Not a dict'}"
                        )
                        return []
                else:
                    return []

            relative_paths = [
                cls._extract_path_from_prefix(absolute_path, prefix)
                for absolute_path in file_list
            ]

            logger.debug(f"Found {len(relative_paths)} files with prefix: {prefix}")
            return relative_paths

        except Exception as e:
            logger.error(
                f"Error listing files in object store with prefix {prefix}: {str(e)}"
            )
            raise e

    @classmethod
    def get_file_data(cls, file_path: str) -> bytes:
        """
        Get raw file data from the object store.

        Args:
            file_path (str): The full path of the file in the object store

        Returns:
            bytes: The raw file data

        Raises:
            Exception: If there's an error getting the file from the object store.
        """
        try:
            metadata = {"key": file_path, "fileName": file_path}
            data = json.dumps({"key": file_path}).encode("utf-8") if file_path else None

            response_data = cls._invoke_dapr_binding(
                operation=cls.OBJECT_GET_OPERATION, metadata=metadata, data=data
            )

            if not response_data:
                raise Exception(f"No data received for file: {file_path}")

            logger.debug(f"Successfully retrieved file data: {file_path}")
            return response_data

        except Exception as e:
            logger.error(f"Error getting file data for {file_path}: {str(e)}")
            raise e
