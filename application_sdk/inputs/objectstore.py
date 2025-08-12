"""Object store interface for the application."""

import json
import os
from typing import Dict, List, Union

import orjson
from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.constants import (
    DAPR_MAX_GRPC_MESSAGE_LENGTH,
    DEPLOYMENT_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class ObjectStoreInput:
    OBJECT_GET_OPERATION = "get"
    OBJECT_LIST_OPERATION = "list"

    @classmethod
    def _invoke_dapr_binding(
        cls,
        operation: str,
        metadata: Dict[str, str],
        data: Union[bytes, str] = "",
        object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
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
            with DaprClient(
                max_grpc_message_length=DAPR_MAX_GRPC_MESSAGE_LENGTH
            ) as client:
                response = client.invoke_binding(
                    binding_name=object_store_name,
                    operation=operation,
                    data=data,
                    binding_metadata=metadata,
                )
                return response.data
        except Exception as e:
            logger.error(f"Error in Dapr binding operation '{operation}': {str(e)}")
            raise

    @classmethod
    def download_files_from_object_store(
        cls,
        download_file_prefix: str,
        file_path: str,
        object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
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
            logger.warning(f"Failed to download files from object store: {str(e)}")
            raise

    @classmethod
    def download_file_from_object_store(
        cls,
        download_file_prefix: str,
        file_path: str,
        object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Downloads a single file from the object store.

        Args:
            download_file_prefix (str): The base path to calculate relative paths from.
                example: /tmp/output
            file_path (str): The full path to where the file should be downloaded.
                example: /tmp/output/persistent-artifacts/apps/myapp/data/wf-123/state.json

        Raises:
            Exception: If there's an error downloading the file from the object store.
        """
        if not os.path.exists(os.path.dirname(file_path)):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

        relative_path = os.path.relpath(file_path, download_file_prefix)

        try:
            # Use get_file_data to retrieve the file bytes
            response_data = cls.get_file_data(relative_path, object_store_name)

            # Write the bytes to the local file
            with open(file_path, "wb") as f:
                f.write(response_data)

            logger.info(f"Successfully downloaded file: {relative_path}")
        except Exception as e:
            logger.warning(
                f"Failed to download file {relative_path} from object store: {str(e)}"
            )
            raise e

    @classmethod
    def list_all_files(
        cls, prefix: str = "", object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> List[str]:
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
            # this takes care of listing from all type of storage - local as well as object stores
            metadata = {"prefix": prefix, "fileName": prefix} if prefix else {}
            data = json.dumps({"prefix": prefix}).encode("utf-8") if prefix else ""

            response_data = cls._invoke_dapr_binding(
                operation=cls.OBJECT_LIST_OPERATION,
                metadata=metadata,
                data=data,
                object_store_name=object_store_name,
            )

            if not response_data:
                return []

            file_list = orjson.loads(response_data.decode("utf-8"))

            # Extract paths based on response type
            if isinstance(file_list, list):
                paths = file_list
            elif isinstance(file_list, dict) and "Contents" in file_list:
                paths = [item["Key"] for item in file_list["Contents"] if "Key" in item]
            elif isinstance(file_list, dict):
                paths = file_list.get("files") or file_list.get("keys") or []
            else:
                return []

            valid_list = []
            for path in paths:
                if not isinstance(path, str):
                    logger.warning(f"Skipping non-string path: {path}")
                    continue
                valid_list.append(
                    path[path.find(prefix) :]
                    if prefix and prefix in path
                    else os.path.basename(path)
                    if prefix
                    else path
                )

            return valid_list

        except Exception as e:
            logger.error(f"Error listing files with prefix {prefix}: {str(e)}")
            raise e

    @classmethod
    def get_file_data(
        cls, file_path: str, object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> bytes:
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
            data = json.dumps({"key": file_path}).encode("utf-8") if file_path else ""

            response_data = cls._invoke_dapr_binding(
                operation=cls.OBJECT_GET_OPERATION,
                metadata=metadata,
                data=data,
                object_store_name=object_store_name,
            )
            if not response_data:
                raise Exception(f"No data received for file: {file_path}")

            logger.debug(f"Successfully retrieved file data: {file_path}")
            return response_data

        except Exception as e:
            logger.error(f"Error getting file data for {file_path}: {str(e)}")
            raise e
