"""Object store interface for the application."""

import os
import shutil

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class ObjectStoreOutput:
    OBJECT_CREATE_OPERATION = "create"

    @staticmethod
    def _cleanup_local_path(path: str) -> None:
        """Remove a file or directory (recursively). Ignores if doesn't exist.
        Args:
            path (str): The path to the file or directory to remove.
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

    @classmethod
    async def push_file_to_object_store(
        cls,
        output_prefix: str,
        file_path: str,
        object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Pushes a single file to the object store.

        Args:
            output_prefix (str): The base path to calculate relative paths from.
                example: /tmp/output
            file_path (str): The full path to the file to be pushed.
                example: /tmp/output/persistent-artifacts/apps/myapp/data/wf-123/state.json

        Raises:
            IOError: If there's an error reading the file.
            Exception: If there's an error pushing the file to the object store.
        """
        with DaprClient() as client:
            try:
                with open(file_path, "rb") as f:
                    file_content = f.read()
            except IOError as e:
                logger.error(f"Error reading file {file_path}: {str(e)}")
                raise e

            relative_path = os.path.relpath(file_path, output_prefix)
            metadata = {
                "key": relative_path,
                "blobName": relative_path,
                "fileName": relative_path,
            }

            try:
                client.invoke_binding(
                    binding_name=object_store_name,
                    operation=cls.OBJECT_CREATE_OPERATION,
                    data=file_content,
                    binding_metadata=metadata,
                )
                logger.debug(f"Successfully pushed file: {relative_path}")
            except Exception as e:
                logger.error(
                    f"Error pushing file {relative_path} to object store: {str(e)}"
                )
                raise e
            cls._cleanup_local_path(file_path)

    @classmethod
    async def push_files_to_object_store(
        cls,
        output_prefix: str,
        input_files_path: str,
        object_store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    ) -> None:
        """Pushes files from a directory to the object store.

        Args:
            output_prefix (str): The base path to calculate relative paths from.
            input_files_path (str): The path to the directory containing files to push.

        Raises:
            ValueError: If the input_files_path doesn't exist or is not a directory.
            IOError: If there's an error reading files.
            Exception: If there's an error with the Dapr client operations.

        Example:
            >>> ObjectStoreOutput.push_files_to_object_store("logs", "/tmp/observability")
        """
        if not os.path.isdir(input_files_path):
            raise ValueError(
                f"The provided output_path '{input_files_path}' is not a valid directory."
            )

        try:
            for root, _, files in os.walk(input_files_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    await cls.push_file_to_object_store(
                        output_prefix, file_path, object_store_name
                    )

            logger.info(
                f"Completed pushing data from {input_files_path} to object store"
            )
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while pushing files to object store: {str(e)}"
            )
            raise e
