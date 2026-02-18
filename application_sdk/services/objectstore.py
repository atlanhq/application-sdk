"""Unified object store interface for the application."""

import json
import os
from typing import List, Union

import orjson
from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.file_ops import SafeFileOps
from application_sdk.constants import (
    DAPR_MAX_GRPC_MESSAGE_LENGTH,
    DEPLOYMENT_OBJECT_STORE_NAME,
    TEMPORARY_PATH,
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

    @staticmethod
    def _as_store_key(path: str) -> str:
        """Normalize a local or object-store path into a clean object store key.

        Accepts either local SDK temporary paths (e.g., ``./local/tmp/artifacts/...``)
        or already-relative object store keys (e.g., ``artifacts/...``).

        Strips the ``./local/tmp/`` prefix when present, converts backslashes to
        forward slashes, and removes leading/trailing slashes.

        Args:
            path: The path to normalize.

        Returns:
            A normalized object store key with forward slashes and no
            leading/trailing slash, or empty string for empty input.
        """
        if not path:
            return ""

        normalized = get_object_store_prefix(path)
        normalized = normalized.replace("\\", "/").replace(os.sep, "/")
        normalized = normalized.strip("/")
        # TEMPORARY_PATH root resolves to "." via os.path.relpath; treat as store root.
        return "" if normalized == "." else normalized

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
            normalized_prefix = cls._as_store_key(prefix)

            # Ensure prefix ends with "/" for the object store query to prevent
            # matching sibling directories (e.g. "orders" matching "orders_archive")
            query_prefix = normalized_prefix
            if normalized_prefix and not normalized_prefix.endswith("/"):
                query_prefix = normalized_prefix + "/"

            data = (
                json.dumps({"prefix": query_prefix}).encode("utf-8")
                if normalized_prefix
                else ""
            )

            response_data = await cls._invoke_dapr_binding(
                operation=cls.OBJECT_LIST_OPERATION,
                metadata=cls._create_list_metadata(query_prefix),
                data=data,
                store_name=store_name,
            )

            if not response_data:
                return []

            file_list = orjson.loads(response_data.decode("utf-8"))

            # Extract paths based on response type
            if isinstance(file_list, list):
                # Handle list format: strings OR Azure Blob/GCP dicts with "Name" field
                paths = []
                for item in file_list:
                    if isinstance(item, str):
                        paths.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("Name"), str):
                        paths.append(item["Name"])
                    else:
                        logger.warning(f"Skipping invalid path entry: {item}")
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

                # Normalize path separators for cross-platform compatibility
                normalized_path = path.replace("\\", "/").replace(os.sep, "/")

                valid_list.append(
                    normalized_path[normalized_path.find(normalized_prefix) :]
                    if normalized_prefix and normalized_prefix in normalized_path
                    else os.path.basename(normalized_path)
                    if normalized_prefix
                    else normalized_path
                )

            return valid_list

        except Exception as e:
            logger.error(
                f"Error listing files with prefix {normalized_prefix or prefix}: {str(e)}"
            )
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
        normalized_key = cls._as_store_key(key)
        try:
            data = json.dumps({"key": normalized_key}).encode("utf-8")

            response_data = await cls._invoke_dapr_binding(
                operation=cls.OBJECT_GET_OPERATION,
                metadata=cls._create_file_metadata(normalized_key),
                data=data,
                store_name=store_name,
            )
            if not response_data:
                if suppress_error:
                    return None
                raise Exception(f"No data received for file: {normalized_key}")

            logger.debug(f"Successfully retrieved file content: {normalized_key}")
            return response_data

        except Exception as e:
            if suppress_error:
                return None
            logger.error(f"Error getting file content for {normalized_key}: {str(e)}")
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
        normalized_key = cls._as_store_key(key)
        try:
            data = json.dumps({"key": normalized_key}).encode("utf-8")

            await cls._invoke_dapr_binding(
                operation=cls.OBJECT_DELETE_OPERATION,
                metadata=cls._create_file_metadata(normalized_key),
                data=data,
                store_name=store_name,
            )
            logger.debug(f"Successfully deleted file: {normalized_key}")
        except Exception as e:
            logger.error(f"Error deleting file {normalized_key}: {str(e)}")
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
        normalized_prefix = cls._as_store_key(prefix)
        try:
            # First, list all files under the prefix
            try:
                files_to_delete = await cls.list_files(
                    prefix=normalized_prefix, store_name=store_name
                )
            except Exception as e:
                # If we can't list files for any reason, we can't delete them either
                # Raise FileNotFoundError to give developers clear feedback
                logger.info(
                    f"Cannot list files under prefix {normalized_prefix}: {str(e)}"
                )
                raise FileNotFoundError(
                    f"No files found under prefix: {normalized_prefix}"
                )

            if not files_to_delete:
                logger.info(f"No files found under prefix: {normalized_prefix}")
                return

            logger.info(
                f"Deleting {len(files_to_delete)} files under prefix: {normalized_prefix}"
            )

            # Delete each file individually
            for file_path in files_to_delete:
                try:
                    await cls.delete_file(key=file_path, store_name=store_name)
                except Exception as e:
                    logger.warning(f"Failed to delete file {file_path}: {str(e)}")
                    # Continue with other files even if one fails

            logger.info(
                f"Successfully deleted all files under prefix: {normalized_prefix}"
            )

        except Exception as e:
            logger.error(
                f"Error deleting files under prefix {normalized_prefix}: {str(e)}"
            )
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

        Args:
            source (str): Local path to the file to upload.
            destination (str): Object store key where the file will be stored.
            store_name (str, optional): Name of the Dapr object store binding to use.
                Defaults to DEPLOYMENT_OBJECT_STORE_NAME.

        Raises:
            IOError: If the source file cannot be read.
            Exception: If there's an error uploading to the object store.

        Example:
            >>> await ObjectStore.upload_file(
            ...     source="/tmp/report.pdf",
            ...     destination="reports/2024/january/report.pdf"
            ... )
        """
        normalized_destination = cls._as_store_key(destination)
        try:
            with SafeFileOps.open(source, "rb") as f:
                file_content = f.read()
        except IOError as e:
            logger.error(f"Error reading file {source}: {str(e)}")
            raise e

        try:
            await cls._invoke_dapr_binding(
                operation=cls.OBJECT_CREATE_OPERATION,
                data=file_content,
                metadata=cls._create_file_metadata(normalized_destination),
                store_name=store_name,
            )
            logger.debug(f"Successfully uploaded file: {normalized_destination}")
        except Exception as e:
            logger.error(
                f"Error uploading file {normalized_destination} to object store: {str(e)}"
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

        Args:
            source (str): Local directory path containing files to upload.
            destination (str): Object store prefix where files will be stored.
            store_name (str, optional): Name of the Dapr object store binding to use.
                Defaults to DEPLOYMENT_OBJECT_STORE_NAME.
            recursive (bool, optional): Whether to include subdirectories.
                Defaults to True.

        Raises:
            ValueError: If the source path is not a valid directory.
            Exception: If there's an error during the upload process.

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
        if not SafeFileOps.isdir(source):
            raise ValueError(f"The provided path '{source}' is not a valid directory.")

        normalized_destination = cls._as_store_key(destination)
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
                    store_key = cls._as_store_key(
                        os.path.join(normalized_destination, relative_path)
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
        normalized_source = cls._as_store_key(source)
        # Ensure directory exists

        if not SafeFileOps.exists(os.path.dirname(destination)):
            SafeFileOps.makedirs(os.path.dirname(destination), exist_ok=True)

        try:
            response_data = await cls.get_content(normalized_source, store_name)

            with SafeFileOps.open(destination, "wb") as f:
                f.write(response_data)

            logger.info(f"Successfully downloaded file: {normalized_source}")
        except Exception as e:
            logger.warning(
                f"Failed to download file {normalized_source} from object store: {str(e)}"
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
        normalized_source = cls._as_store_key(source)
        try:
            # List all files under the prefix
            file_list = await cls.list_files(normalized_source, store_name)

            logger.info(
                f"Found {len(file_list)} files to download from: {normalized_source}"
            )

            # Normalize source prefix to use forward slashes for comparison
            # Download each file
            for file_path in file_list:
                normalized_file_path = file_path.replace("\\", "/").replace(os.sep, "/")
                if normalized_file_path.startswith(normalized_source):
                    # Extract relative path after the prefix
                    relative_path = normalized_file_path[
                        len(normalized_source) :
                    ].lstrip("/")
                else:
                    # Fallback to just the filename
                    relative_path = os.path.basename(normalized_file_path)

                local_file_path = os.path.join(destination, relative_path)
                await cls.download_file(file_path, local_file_path, store_name)

            logger.info(f"Successfully downloaded all files from: {normalized_source}")
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
            # Calculate data size (handle both bytes and str)
            if isinstance(data, bytes):
                data_size = len(data)
            elif isinstance(data, str):
                data_size = len(data.encode("utf-8"))
            else:
                data_size = 0

            # Check if data size exceeds DAPR limit and log warning
            if data_size > DAPR_MAX_GRPC_MESSAGE_LENGTH:
                # gRPC adds overhead (headers, metadata, etc.) to messages
                # Add a buffer of 5% or at least 1KB to account for this overhead
                grpc_overhead_buffer = max(int(data_size * 0.05), 1024)
                required_max_length = data_size + grpc_overhead_buffer
                logger.warning(
                    f"Data size ({data_size:,} bytes / {data_size / (1024 * 1024):.2f}MB) "
                    f"exceeds DAPR_MAX_GRPC_MESSAGE_LENGTH ({DAPR_MAX_GRPC_MESSAGE_LENGTH:,} bytes / "
                    f"{DAPR_MAX_GRPC_MESSAGE_LENGTH / (1024 * 1024):.2f}MB). "
                    f"Increasing max_grpc_message_length to {required_max_length:,} bytes "
                    f"(data size + {grpc_overhead_buffer:,} bytes overhead buffer). "
                    f"This may cause issues with Dapr gRPC communication."
                )
                # Set max_grpc_message_length to accommodate the data size plus gRPC overhead
                max_message_length = required_max_length
            else:
                # Data is within limit, use default DAPR limit
                max_message_length = DAPR_MAX_GRPC_MESSAGE_LENGTH

            with DaprClient(max_grpc_message_length=max_message_length) as client:
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
            if SafeFileOps.isfile(path) or os.path.islink(path):
                SafeFileOps.remove(path)
            elif SafeFileOps.isdir(path):
                SafeFileOps.rmtree(path)
        except FileNotFoundError:
            pass  # ignore if the file or directory doesn't exist
        except Exception as e:
            logger.warning(f"Error cleaning up {path}: {str(e)}")
