"""Object store interface for the application."""

import os

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.constants import OBJECT_STORE_NAME

activity.logger = get_logger(__name__)


class ObjectStoreInput:
    OBJECT_GET_OPERATION = "get"

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
                    binding_name=OBJECT_STORE_NAME,
                    operation=cls.OBJECT_GET_OPERATION,
                    binding_metadata=metadata,
                )
                with open(file_path, "w") as f:
                    f.write(response.data.decode("utf-8"))
                    f.close()

                activity.logger.debug(f"Successfully downloaded file: {relative_path}")
            except Exception as e:
                activity.logger.error(
                    f"Error downloading file {relative_path} to object store: {str(e)}"
                )
                raise e
