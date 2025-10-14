"""Unified object store interface for the application."""

import json
import os
import shutil
from typing import List, Union

import orjson
from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.constants import (
    DAPR_MAX_GRPC_MESSAGE_LENGTH,
    DEPLOYMENT_OBJECT_STORE_NAME,
    ENABLE_ATLAN_UPLOAD,
    TEMPORARY_PATH,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class ObjectStore:
    """Unified object store interface supporting both file and directory operations."""

    OBJECT_CREATE_OPERATION = "create"
    OBJECT_GET_OPERATION = "get"
    OBJECT_LIST_OPERATION = "list"
    OBJECT_DELETE_OPERATION = "delete"

    @classmethod
    def _create_file_metadata(cls, key: str) -> dict[str, str]:
        """Create metadata for file operations (get, delete, create).

        Args:
            key: The file key/path.

        Returns:
            Metadata dictionary with key, fileName, and blobName fields.
        """
        return {"key": key, "fileName": key, "blobName": key}

    @classmethod
    def _create_list_metadata(cls, prefix: str) -> dict[str, str]:
        """Create metadata for list operations.

        Args:
            prefix: The prefix to list files under.

        Returns:
            Metadata dictionary with prefix and fileName fields, or empty dict if no prefix.
        """
        return {"prefix": prefix, "fileName": prefix} if prefix else {}

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
            data = json.dumps({"prefix": prefix}).encode("utf-8") if prefix else ""

            response_data = await cls._invoke_dapr_binding(
                operation=cls.OBJECT_LIST_OPERATION,
                metadata=cls._create_list_metadata(prefix),
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
        cls,
        key: str,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
        suppress_error: bool = False,
    ) -> bytes | None:
        """Get raw file content from the object store.

        Args:
            key: The path of the file in the object store.
            store_name: Name of the Dapr object store binding to use.
            suppress_error: Whether to suppress the error and return None if the file does not exist.
        Returns:
            The raw file content as bytes.

        Raises:
            Exception: If there's an error getting the file from the object store.
        """
        try:
            data = json.dumps({"key": key}).encode("utf-8") if key else ""

            response_data = await cls._invoke_dapr_binding(
                operation=cls.OBJECT_GET_OPERATION,
                metadata=cls._create_file_metadata(key),
                data=data,
                store_name=store_name,
            )
            if not response_data:
                if suppress_error:
                    return None
                raise Exception(f"No data received for file: {key}")

            logger.debug(f"Successfully retrieved file content: {key}")
            return response_data

        except Exception as e:
            if suppress_error:
                return None
            logger.error(f"Error getting file content for {key}: {str(e)}")
            raise

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
    async def delete_file(
        cls, key: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> None:
        """Delete a single file from the object store.

        Args:
            key: The file path to delete.
            store_name: Name of the Dapr object store binding to use.

        Raises:
            Exception: If there's an error deleting the file from the object store.
        """
        try:
            data = json.dumps({"key": key}).encode("utf-8")

            await cls._invoke_dapr_binding(
                operation=cls.OBJECT_DELETE_OPERATION,
                metadata=cls._create_file_metadata(key),
                data=data,
                store_name=store_name,
            )
            logger.debug(f"Successfully deleted file: {key}")
        except Exception as e:
            logger.error(f"Error deleting file {key}: {str(e)}")
            raise

    @classmethod
    async def delete_prefix(
        cls, prefix: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> None:
        """Delete all files under a prefix from the object store.

        Args:
            prefix: The prefix path to delete all files under.
            store_name: Name of the Dapr object store binding to use.

        Raises:
            Exception: If there's an error deleting files from the object store.
        """
        try:
            # First, list all files under the prefix
            try:
                files_to_delete = await cls.list_files(
                    prefix=prefix, store_name=store_name
                )
            except Exception as e:
                # If we can't list files for any reason, we can't delete them either
                # Raise FileNotFoundError to give developers clear feedback
                logger.info(f"Cannot list files under prefix {prefix}: {str(e)}")
                raise FileNotFoundError(f"No files found under prefix: {prefix}")

            if not files_to_delete:
                logger.info(f"No files found under prefix: {prefix}")
                return

            logger.info(f"Deleting {len(files_to_delete)} files under prefix: {prefix}")

            # Delete each file individually
            for file_path in files_to_delete:
                try:
                    await cls.delete_file(key=file_path, store_name=store_name)
                except Exception as e:
                    logger.warning(f"Failed to delete file {file_path}: {str(e)}")
                    # Continue with other files even if one fails

            logger.info(f"Successfully deleted all files under prefix: {prefix}")

        except Exception as e:
            logger.error(f"Error deleting files under prefix {prefix}: {str(e)}")
            raise

    @classmethod
    async def upload_file(
        cls,
        source: str,
        destination: str,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
        retain_local_copy: bool = False,
    ) -> None:
        """Upload a single file to the object store.

        This method performs a primary upload to the specified store. If ENABLE_ATLAN_UPLOAD
        is enabled and the primary store is not the upstream store, it will also perform
        a secondary upload to the upstream store.

        Args:
            source (str): Local path to the file to upload.
            destination (str): Object store key where the file will be stored.
            store_name (str, optional): Name of the Dapr object store binding to use.
                Defaults to DEPLOYMENT_OBJECT_STORE_NAME.
            retain_local_copy (bool, optional): Whether to keep the local file after upload.
                Defaults to False.

        Raises:
            IOError: If the source file cannot be read.
            Exception: If there's an error uploading to the primary object store.

        Note:
            - If ENABLE_ATLAN_UPLOAD is True, files are also uploaded to UPSTREAM_OBJECT_STORE_NAME
            - Upstream upload failures are logged as warnings but don't fail the operation
            - The local file is cleaned up after successful primary upload (unless retain_local_copy=True)

        Example:
            >>> await ObjectStore.upload_file(
            ...     source="/tmp/report.pdf",
            ...     destination="reports/2024/january/report.pdf"
            ... )
        """
        try:
            with open(source, "rb") as f:
                file_content = f.read()
        except IOError as e:
            logger.error(f"Error reading file {source}: {str(e)}")
            raise e

        try:
            # Primary upload to the specified store
            await cls._invoke_dapr_binding(
                operation=cls.OBJECT_CREATE_OPERATION,
                data=file_content,
                metadata=cls._create_file_metadata(destination),
                store_name=store_name,
            )
            logger.debug(f"Successfully uploaded file to {store_name}: {destination}")
            
            # Secondary upload to upstream store if enabled
            if ENABLE_ATLAN_UPLOAD and store_name != UPSTREAM_OBJECT_STORE_NAME:
                try:
                    await cls._invoke_dapr_binding(
                        operation=cls.OBJECT_CREATE_OPERATION,
                        data=file_content,
                        metadata=cls._create_file_metadata(destination),
                        store_name=UPSTREAM_OBJECT_STORE_NAME,
                    )
                    logger.debug(f"Successfully uploaded file to upstream store {UPSTREAM_OBJECT_STORE_NAME}: {destination}")
                except Exception as upstream_error:
                    # Log upstream upload error but don't fail the entire operation
                    logger.warning(
                        f"Failed to upload file to upstream store {UPSTREAM_OBJECT_STORE_NAME}: {str(upstream_error)}"
                    )
                    
        except Exception as e:
            logger.error(
                f"Error uploading file {destination} to object store: {str(e)}"
            )
            raise e

        # Clean up local file after successful upload
        if not retain_local_copy:
            cls._cleanup_local_path(source)

    @classmethod
    async def upload_prefix(
        cls,
        source: str,
        destination: str,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
        recursive: bool = True,
        retain_local_copy: bool = False,
    ) -> None:
        """Upload all files from a directory to the object store.

        This method uploads all files in a directory using upload_file, which means
        it inherits the dual upload functionality (primary + upstream if enabled).

        Args:
            source (str): Local directory path containing files to upload.
            destination (str): Object store prefix where files will be stored.
            store_name (str, optional): Name of the Dapr object store binding to use.
                Defaults to DEPLOYMENT_OBJECT_STORE_NAME.
            recursive (bool, optional): Whether to include subdirectories.
                Defaults to True.
            retain_local_copy (bool, optional): Whether to keep local files after upload.
                Defaults to False.

        Raises:
            ValueError: If the source path is not a valid directory.
            Exception: If there's an error during the upload process.

        Note:
            - Each file is uploaded using upload_file, which supports dual upload
            - If ENABLE_ATLAN_UPLOAD is True, files are also uploaded to upstream store

        Example:
            >>> # Upload all files recursively
            >>> await ObjectStore.upload_prefix(
            ...     source="local/project/",
            ...     destination="backups/project-v1/",
            ...     recursive=True
            ... )

            >>> # Upload only root level files
            >>> await ObjectStore.upload_prefix(
            ...     source="local/logs/",
            ...     destination="daily-logs/",
            ...     recursive=False
            ... )
        """
        if not os.path.isdir(source):
            raise ValueError(f"The provided path '{source}' is not a valid directory.")

        try:
            for root, _, files in os.walk(source):
                # Skip subdirectories if not recursive
                if not recursive and root != source:
                    continue

                for file in files:
                    file_path = os.path.join(root, file)
                    # Calculate relative path from the base directory
                    relative_path = os.path.relpath(file_path, source)
                    # Create store key by combining prefix with relative path
                    store_key = os.path.join(destination, relative_path).replace(
                        os.sep, "/"
                    )
                    await cls.upload_file(
                        file_path, store_key, store_name, retain_local_copy
                    )

            logger.info(f"Completed uploading directory {source} to object store")
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while uploading directory: {str(e)}"
            )
            raise e

    @classmethod
    async def download_file(
        cls,
        source: str,
        destination: str,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Download a single file from the object store.

        Args:
            source (str): Object store key of the file to download.
            destination (str): Local path where the file will be saved.
            store_name (str, optional): Name of the Dapr object store binding to use.
                Defaults to DEPLOYMENT_OBJECT_STORE_NAME.

        Raises:
            Exception: If there's an error downloading from the object store.

        Note:
            The destination directory will be created automatically if it doesn't exist.

        Example:
            >>> await ObjectStore.download_file(
            ...     source="reports/2024/january/report.pdf",
            ...     destination="/tmp/downloaded_report.pdf"
            ... )
        """
        # Ensure directory exists

        if not os.path.exists(os.path.dirname(destination)):
            os.makedirs(os.path.dirname(destination), exist_ok=True)

        try:
            response_data = await cls.get_content(source, store_name)

            with open(destination, "wb") as f:
                f.write(response_data)

            logger.info(f"Successfully downloaded file: {source}")
        except Exception as e:
            logger.warning(
                f"Failed to download file {source} from object store: {str(e)}"
            )
            raise e

    @classmethod
    async def download_prefix(
        cls,
        source: str,
        destination: str = TEMPORARY_PATH,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Download all files from a store prefix to a local directory.

        Args:
            source: Object store prefix to download files from.
            destination: Local directory where files will be saved.
            store_name: Name of the Dapr object store binding to use.
        """
        try:
            # List all files under the prefix
            file_list = await cls.list_files(source, store_name)

            logger.info(f"Found {len(file_list)} files to download from: {source}")

            # Download each file
            for file_path in file_list:
                local_file_path = os.path.join(destination, file_path)
                await cls.download_file(file_path, local_file_path, store_name)

            logger.info(f"Successfully downloaded all files from: {source}")
        except Exception as e:
            logger.warning(f"Failed to download files from object store: {str(e)}")
            raise

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
        except Exception:
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
