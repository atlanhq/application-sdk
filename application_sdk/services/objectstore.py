"""Unified object store interface for the application."""

import json
import os
import shutil
from typing import List, Union, overload

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


class ObjectStore:
    """Unified object store interface supporting both file and directory operations."""

    OBJECT_CREATE_OPERATION = "create"
    OBJECT_GET_OPERATION = "get"
    OBJECT_LIST_OPERATION = "list"

    @overload
    @classmethod
    async def upload(
        cls,
        source: str,
        destination: str,
        *,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Upload single file to object store."""
        ...

    @overload
    @classmethod
    async def upload(
        cls,
        source: str,
        destination: str,
        *,
        recursive: bool = True,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Upload directory to object store."""
        ...

    @classmethod
    async def upload(
        cls,
        source: str,
        destination: str,
        *,
        recursive: bool = True,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Upload file or directory to object store.

        Automatically detects whether source is a file or directory and handles accordingly.

        Args:
            source: Local file path or directory path to upload.
            destination: Object store key (for files) or prefix (for directories) where content will be stored.
            recursive: For directories, whether to recursively upload subdirectories (default: True).
            store_name: Name of the Dapr object store binding to use.

        Raises:
            ValueError: If source path does not exist.
            IOError: If there's an error reading files.
            Exception: If there's an error with object store operations.

        Examples:
            >>> await ObjectStore.upload("/local/data.json", "workflows/123/data.json")
            >>> await ObjectStore.upload("/local/reports/", "workflows/123/reports/")
        """
        if not os.path.exists(source):
            raise ValueError(f"Source path does not exist: {source}")

        if os.path.isfile(source):
            await cls._upload_file(source, destination, store_name)
        elif os.path.isdir(source):
            await cls._upload_directory(source, destination, recursive, store_name)
        else:
            raise ValueError(f"Source path is neither file nor directory: {source}")

    @overload
    @classmethod
    async def download(
        cls,
        source: str,
        destination: str,
        *,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Download single file from object store."""
        ...

    @overload
    @classmethod
    async def download(
        cls,
        source: str,
        destination: str,
        *,
        recursive: bool = True,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Download directory from object store."""
        ...

    @classmethod
    async def download(
        cls,
        source: str,
        destination: str,
        *,
        recursive: bool = True,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Download file or directory from object store.

        Automatically detects whether source is a single file or directory prefix.

        Args:
            source: Object store key (for files) or prefix (for directories) to download.
            destination: Local file path or directory path where content will be saved.
            recursive: For directories, whether to recursively download subdirectories (default: True).
            store_name: Name of the Dapr object store binding to use.

        Raises:
            Exception: If there's an error downloading from object store.

        Examples:
            >>> await ObjectStore.download("workflows/123/data.json", "/local/data.json")
            >>> await ObjectStore.download("workflows/123/reports/", "/local/reports/")
        """
        if await cls._is_single_file(source, store_name):
            await cls._download_file(source, destination, store_name)
        else:
            await cls._download_directory(source, destination, recursive, store_name)

    @classmethod
    async def list_files(
        cls, prefix: str = "", store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> List[str]:
        """List all files in the object store under a given prefix.

        Args:
            prefix: The prefix to filter files. Empty string returns all files.
            store_name: Name of the Dapr object store binding to use.

        Returns:
            List of file paths in the object store.

        Raises:
            Exception: If there's an error listing files from the object store.
        """
        try:
            metadata = {"prefix": prefix, "fileName": prefix} if prefix else {}
            data = json.dumps({"prefix": prefix}).encode("utf-8") if prefix else ""

            response_data = await cls._invoke_dapr_binding(
                operation=cls.OBJECT_LIST_OPERATION,
                metadata=metadata,
                data=data,
                store_name=store_name,
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
    async def get_content(
        cls, key: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> bytes:
        """Get raw file content from the object store.

        Args:
            key: The path of the file in the object store.
            store_name: Name of the Dapr object store binding to use.

        Returns:
            The raw file content as bytes.

        Raises:
            Exception: If there's an error getting the file from the object store.
        """
        try:
            metadata = {"key": key, "fileName": key}
            data = json.dumps({"key": key}).encode("utf-8") if key else ""

            response_data = await cls._invoke_dapr_binding(
                operation=cls.OBJECT_GET_OPERATION,
                metadata=metadata,
                data=data,
                store_name=store_name,
            )
            if not response_data:
                raise Exception(f"No data received for file: {key}")

            logger.debug(f"Successfully retrieved file content: {key}")
            return response_data

        except Exception as e:
            logger.error(f"Error getting file content for {key}: {str(e)}")
            raise e

    @classmethod
    async def exists(
        cls, key: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> bool:
        """Check if a file exists in the object store.

        Args:
            key: The path of the file in the object store.
            store_name: Name of the Dapr object store binding to use.

        Returns:
            True if the file exists, False otherwise.
        """
        try:
            await cls.get_content(key, store_name)
            return True
        except Exception:
            return False

    @classmethod
    async def delete(
        cls, key: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> None:
        """Delete a file or all files under a prefix from the object store.

        Args:
            key: The file path or prefix to delete.
            store_name: Name of the Dapr object store binding to use.

        Note:
            This method is not implemented as it's not commonly used in the current codebase.
            Can be implemented when needed based on the underlying object store capabilities.
        """
        raise NotImplementedError("Delete operation not yet implemented")

    @classmethod
    async def _upload_file(
        cls, file_path: str, store_key: str, store_name: str
    ) -> None:
        """Upload a single file to the object store.

        Args:
            file_path: Local path to the file to upload.
            store_key: Object store key where the file will be stored.
            store_name: Name of the Dapr object store binding to use.
        """
        try:
            with open(file_path, "rb") as f:
                file_content = f.read()
        except IOError as e:
            logger.error(f"Error reading file {file_path}: {str(e)}")
            raise e

        metadata = {
            "key": store_key,
            "blobName": store_key,
            "fileName": store_key,
        }

        try:
            await cls._invoke_dapr_binding(
                operation=cls.OBJECT_CREATE_OPERATION,
                data=file_content,
                metadata=metadata,
                store_name=store_name,
            )
            logger.debug(f"Successfully uploaded file: {store_key}")
        except Exception as e:
            logger.error(f"Error uploading file {store_key} to object store: {str(e)}")
            raise e

        # Clean up local file after successful upload
        cls._cleanup_local_path(file_path)

    @classmethod
    async def _upload_directory(
        cls, directory_path: str, store_prefix: str, recursive: bool, store_name: str
    ) -> None:
        """Upload all files from a directory to the object store.

        Args:
            directory_path: Local directory path containing files to upload.
            store_prefix: Object store prefix where files will be stored.
            recursive: Whether to recursively upload subdirectories.
            store_name: Name of the Dapr object store binding to use.
        """
        if not os.path.isdir(directory_path):
            raise ValueError(
                f"The provided path '{directory_path}' is not a valid directory."
            )

        try:
            for root, _, files in os.walk(directory_path):
                # Skip subdirectories if not recursive
                if not recursive and root != directory_path:
                    continue

                for file in files:
                    file_path = os.path.join(root, file)
                    # Calculate relative path from the base directory
                    relative_path = os.path.relpath(file_path, directory_path)
                    # Create store key by combining prefix with relative path
                    store_key = os.path.join(store_prefix, relative_path).replace(
                        os.sep, "/"
                    )
                    await cls._upload_file(file_path, store_key, store_name)

            logger.info(
                f"Completed uploading directory {directory_path} to object store"
            )
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while uploading directory: {str(e)}"
            )
            raise e

    @classmethod
    async def _download_file(
        cls, store_key: str, file_path: str, store_name: str
    ) -> None:
        """Download a single file from the object store.

        Args:
            store_key: Object store key of the file to download.
            file_path: Local path where the file will be saved.
            store_name: Name of the Dapr object store binding to use.
        """
        # Ensure directory exists
        if not os.path.exists(os.path.dirname(file_path)):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

        try:
            response_data = await cls.get_content(store_key, store_name)

            with open(file_path, "wb") as f:
                f.write(response_data)

            logger.info(f"Successfully downloaded file: {store_key}")
        except Exception as e:
            logger.warning(
                f"Failed to download file {store_key} from object store: {str(e)}"
            )
            raise e

    @classmethod
    async def _download_directory(
        cls, store_prefix: str, directory_path: str, recursive: bool, store_name: str
    ) -> None:
        """Download all files from a store prefix to a local directory.

        Args:
            store_prefix: Object store prefix to download files from.
            directory_path: Local directory where files will be saved.
            recursive: Whether to recursively download subdirectories.
            store_name: Name of the Dapr object store binding to use.
        """
        try:
            # List all files under the prefix
            file_list = await cls.list_files(store_prefix, store_name)

            logger.info(
                f"Found {len(file_list)} files to download from: {store_prefix}"
            )

            # Download each file
            for relative_path in file_list:
                # Skip subdirectories if not recursive
                if not recursive and "/" in relative_path.strip("/"):
                    continue

                store_key = os.path.join(store_prefix, relative_path).replace(
                    os.sep, "/"
                )
                local_file_path = os.path.join(directory_path, relative_path)
                await cls._download_file(store_key, local_file_path, store_name)

            logger.info(f"Successfully downloaded all files from: {store_prefix}")
        except Exception as e:
            logger.warning(f"Failed to download files from object store: {str(e)}")
            raise

    @classmethod
    async def _is_single_file(cls, store_key: str, store_name: str) -> bool:
        """Check if the store key represents a single file or a directory prefix.

        Args:
            store_key: Object store key to check.
            store_name: Name of the Dapr object store binding to use.

        Returns:
            True if it's a single file, False if it's a directory prefix.
        """
        try:
            # Try to get content directly - if successful, it's a single file
            await cls.get_content(store_key, store_name)
            return True
        except Exception:
            # If that fails, check if there are files under this prefix
            files = await cls.list_files(store_key, store_name)
            return (
                len(files) == 0
            )  # If no files found, assume it's meant to be a single file

    @classmethod
    async def _invoke_dapr_binding(
        cls,
        operation: str,
        metadata: dict,
        data: Union[bytes, str] = "",
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> bytes:
        """Common method to invoke Dapr binding operations.

        Args:
            operation: The Dapr binding operation to perform.
            metadata: Metadata for the binding operation.
            data: Optional data to send with the request.
            store_name: Name of the Dapr object store binding to use.

        Returns:
            Response data from the Dapr binding.

        Raises:
            Exception: If there's an error with the Dapr binding operation.
        """
        try:
            with DaprClient(
                max_grpc_message_length=DAPR_MAX_GRPC_MESSAGE_LENGTH
            ) as client:
                response = client.invoke_binding(
                    binding_name=store_name,
                    operation=operation,
                    data=data,
                    binding_metadata=metadata,
                )
                return response.data
        except Exception as e:
            logger.error(f"Error in Dapr binding operation '{operation}': {str(e)}")
            raise

    @classmethod
    def _cleanup_local_path(cls, path: str) -> None:
        """Remove a file or directory (recursively). Ignores if doesn't exist.

        Args:
            path: The path to the file or directory to remove.
        """
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except FileNotFoundError:
            pass  # ignore if the file or directory doesn't exist
        except Exception as e:
            logger.warning(f"Error cleaning up {path}: {str(e)}")
