"""Unified object store interface backed by obstore.

Drop-in replacement for the Dapr-based ObjectStore. Same public API,
same method signatures, same behavior — but uses obstore (Rust-backed)
instead of Dapr gRPC bindings.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List

from application_sdk.common.file_ops import SafeFileOps
from application_sdk.constants import (
    DEPLOYMENT_OBJECT_STORE_NAME,
    TEMPORARY_PATH,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.storage._ops import delete
from application_sdk.services.storage._ops import exists as _exists
from application_sdk.services.storage._ops import (
    get_bytes,
    head,
    list_paths,
    put,
    stream_download,
    stream_upload,
)
from application_sdk.services.storage._registry import StoreRegistry

logger = get_logger(__name__)

# Size threshold for switching to streaming I/O (64 MB)
_STREAM_THRESHOLD = 64 * 1024 * 1024

# Max concurrent operations for prefix methods
_MAX_CONCURRENCY = int(os.getenv("ATLAN_OBJECT_STORE_MAX_CONCURRENCY", "10"))


class ObjectStore:
    """Unified object store interface supporting both file and directory operations.

    All methods are async classmethods — no instantiation needed.
    Backward-compatible with the Dapr-based ObjectStore API.
    """

    OBJECT_CREATE_OPERATION = "create"
    OBJECT_GET_OPERATION = "get"
    OBJECT_LIST_OPERATION = "list"
    OBJECT_DELETE_OPERATION = "delete"

    @staticmethod
    def as_store_key(path: str) -> str:
        """Normalize a local or object-store path into a clean object store key.

        Accepts any of:
        - Local SDK temporary paths (e.g., ``./local/tmp/artifacts/...``)
        - Absolute paths (e.g., ``/data/test.parquet`` becomes ``data/test.parquet``)
        - Already-relative object store keys (e.g., ``artifacts/...``)

        Returns:
            A normalized object store key with forward slashes and no
            leading/trailing slash, or empty string for empty input.
        """
        if not path:
            return ""

        abs_path = os.path.abspath(path)
        abs_temp_path = os.path.abspath(TEMPORARY_PATH)
        try:
            common_path = os.path.commonpath([abs_path, abs_temp_path])
            if common_path == abs_temp_path:
                normalized = os.path.relpath(abs_path, abs_temp_path).replace(
                    os.path.sep, "/"
                )
            else:
                normalized = path.strip("/")
        except ValueError:
            normalized = path.strip("/")

        normalized = normalized.replace("\\", "/").replace(os.sep, "/")
        normalized = normalized.strip("/")
        return "" if normalized == "." else normalized

    @classmethod
    async def list_files(
        cls, prefix: str = "", store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> List[str]:
        """List all files in the object store under a given prefix.

        Args:
            prefix: The prefix to filter files. Empty string returns all files.
            store_name: Name of the object store binding to use.

        Returns:
            List of file paths in the object store.
        """
        normalized_prefix = cls.as_store_key(prefix)
        try:
            store = await StoreRegistry.get_store(store_name)

            query_prefix = normalized_prefix
            if normalized_prefix and not normalized_prefix.endswith("/"):
                query_prefix = normalized_prefix + "/"

            paths = await list_paths(store, query_prefix)

            # Normalize returned paths to match current behavior:
            # paths from obstore are relative to the store root
            valid_list = []
            for p in paths:
                normalized_path = p.replace("\\", "/").replace(os.sep, "/")
                if normalized_prefix and normalized_prefix in normalized_path:
                    valid_list.append(
                        normalized_path[normalized_path.find(normalized_prefix) :]
                    )
                elif normalized_prefix:
                    valid_list.append(os.path.basename(normalized_path))
                else:
                    valid_list.append(normalized_path)

            return valid_list

        except Exception as e:
            logger.error(
                f"Error listing files with prefix {normalized_prefix or prefix}: {str(e)}"
            )
            raise

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
            store_name: Name of the object store binding to use.
            suppress_error: If True, return None instead of raising on errors.

        Returns:
            The raw file content as bytes, or None if suppress_error is True and an error occurs.
        """
        normalized_key = cls.as_store_key(key)
        try:
            store = await StoreRegistry.get_store(store_name)
            data = await get_bytes(store, normalized_key)
            if not data:
                if suppress_error:
                    return None
                raise Exception(f"No data received for file: {normalized_key}")
            logger.debug(f"Successfully retrieved file content: {normalized_key}")
            return data
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

        Uses HEAD request — no data transfer.
        """
        normalized_key = cls.as_store_key(key)
        try:
            store = await StoreRegistry.get_store(store_name)
            return await _exists(store, normalized_key)
        except Exception:
            return False

    @classmethod
    async def delete_file(
        cls, key: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> None:
        """Delete a single file from the object store."""
        normalized_key = cls.as_store_key(key)
        try:
            store = await StoreRegistry.get_store(store_name)
            await delete(store, normalized_key)
            logger.debug(f"Successfully deleted file: {normalized_key}")
        except Exception as e:
            logger.error(f"Error deleting file {normalized_key}: {str(e)}")
            raise

    @classmethod
    async def delete_prefix(
        cls, prefix: str, store_name: str = DEPLOYMENT_OBJECT_STORE_NAME
    ) -> None:
        """Delete all files under a prefix from the object store."""
        normalized_prefix = cls.as_store_key(prefix)
        try:
            try:
                files_to_delete = await cls.list_files(
                    prefix=normalized_prefix, store_name=store_name
                )
            except Exception as e:
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

            store = await StoreRegistry.get_store(store_name)
            semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

            async def _delete_one(file_path: str) -> None:
                async with semaphore:
                    try:
                        await delete(store, file_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete file {file_path}: {str(e)}")

            await asyncio.gather(*[_delete_one(fp) for fp in files_to_delete])
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

        Uses streaming for large files (>64MB) to avoid memory issues.

        Args:
            source: Local path to the file to upload.
            destination: Object store key where the file will be stored.
            store_name: Name of the object store binding to use.
            retain_local_copy: If False (default), delete local file after upload.
        """
        normalized_destination = cls.as_store_key(destination)
        try:
            store = await StoreRegistry.get_store(store_name)
            source_path = Path(source)

            if not source_path.exists():
                raise IOError(f"Source file not found: {source}")

            file_size = source_path.stat().st_size

            if file_size > _STREAM_THRESHOLD:
                await stream_upload(store, source_path, normalized_destination)
            else:
                with SafeFileOps.open(source, "rb") as f:
                    file_content = f.read()
                await put(store, normalized_destination, file_content)

            logger.debug(f"Successfully uploaded file: {normalized_destination}")
        except IOError:
            raise
        except Exception as e:
            logger.error(
                f"Error uploading file {normalized_destination} to object store: {str(e)}"
            )
            raise

        if not retain_local_copy:
            cls._cleanup_local_path(source)

    @classmethod
    async def upload_file_from_bytes(
        cls,
        file_content: bytes,
        destination: str,
        store_name: str = UPSTREAM_OBJECT_STORE_NAME,
    ) -> None:
        """Upload file content directly from bytes to object store."""
        try:
            store = await StoreRegistry.get_store(store_name)
            await put(store, destination, file_content)
            logger.debug(f"Successfully uploaded file from bytes: {destination}")
        except Exception as e:
            logger.error(
                f"Error uploading file from bytes {destination} to object store: {str(e)}"
            )
            raise

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
            source: Local directory path containing files to upload.
            destination: Object store prefix where files will be stored.
            store_name: Name of the object store binding to use.
            recursive: Whether to include subdirectories.
            retain_local_copy: If False (default), delete local files after upload.
        """
        if not SafeFileOps.isdir(source):
            raise ValueError(f"The provided path '{source}' is not a valid directory.")

        normalized_destination = cls.as_store_key(destination)
        try:
            semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

            async def _upload_one(file_path: str, store_key: str) -> None:
                async with semaphore:
                    await cls.upload_file(
                        file_path, store_key, store_name, retain_local_copy
                    )

            tasks = []
            for root, _, files in os.walk(source):
                if not recursive and root != source:
                    continue
                for file in files:
                    file_path = os.path.join(root, file)
                    relative_path = os.path.relpath(file_path, source)
                    store_key = cls.as_store_key(
                        os.path.join(normalized_destination, relative_path)
                    )
                    tasks.append(_upload_one(file_path, store_key))

            await asyncio.gather(*tasks)
            logger.info(f"Completed uploading directory {source} to object store")
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while uploading directory: {str(e)}"
            )
            raise

    @classmethod
    async def download_file(
        cls,
        source: str,
        destination: str,
        store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Download a single file from the object store.

        Uses streaming for large files to avoid memory issues.

        Args:
            source: Object store key of the file to download.
            destination: Local path where the file will be saved.
            store_name: Name of the object store binding to use.
        """
        normalized_source = cls.as_store_key(source)

        if not SafeFileOps.exists(os.path.dirname(destination)):
            SafeFileOps.makedirs(os.path.dirname(destination), exist_ok=True)

        try:
            store = await StoreRegistry.get_store(store_name)

            # Try HEAD to check size for streaming decision
            try:
                meta = await head(store, normalized_source)
                file_size = meta.get("size", 0)
            except Exception:
                file_size = 0

            if file_size > _STREAM_THRESHOLD:
                await stream_download(store, normalized_source, Path(destination))
            else:
                response_data = await get_bytes(store, normalized_source)
                with SafeFileOps.open(destination, "wb") as f:
                    f.write(response_data)

            logger.info(f"Successfully downloaded file: {normalized_source}")
        except Exception as e:
            logger.warning(
                f"Failed to download file {normalized_source} from object store: {str(e)}"
            )
            raise

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
            store_name: Name of the object store binding to use.
        """
        normalized_source = cls.as_store_key(source)
        try:
            file_list = await cls.list_files(normalized_source, store_name)

            logger.info(
                f"Found {len(file_list)} files to download from: {normalized_source}"
            )

            semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

            async def _download_one(file_path: str, local_path: str) -> None:
                async with semaphore:
                    await cls.download_file(file_path, local_path, store_name)

            tasks = []
            for file_path in file_list:
                normalized_file_path = file_path.replace("\\", "/").replace(os.sep, "/")
                if normalized_file_path.startswith(normalized_source):
                    relative_path = normalized_file_path[
                        len(normalized_source) :
                    ].lstrip("/")
                else:
                    relative_path = os.path.basename(normalized_file_path)

                local_file_path = os.path.join(destination, relative_path)
                tasks.append(_download_one(file_path, local_file_path))

            await asyncio.gather(*tasks)
            logger.info(f"Successfully downloaded all files from: {normalized_source}")
        except Exception as e:
            logger.warning(f"Failed to download files from object store: {str(e)}")
            raise

    @classmethod
    def _cleanup_local_path(cls, path: str) -> None:
        """Remove a file or directory (recursively). Ignores if doesn't exist."""
        try:
            if SafeFileOps.isfile(path) or os.path.islink(path):
                SafeFileOps.remove(path)
            elif SafeFileOps.isdir(path):
                SafeFileOps.rmtree(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Error cleaning up {path}: {str(e)}")
