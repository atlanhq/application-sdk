"""Object store interface for the application."""
import logging
import os

from dapr.clients import DaprClient

logger = logging.getLogger(__name__)


class ObjectStore:
    OBJECT_STORE_NAME = "objectstore"
    OBJECT_CREATE_OPERATION = "create"

    @classmethod
    def push_to_object_store(cls, output_prefix: str, output_path: str) -> None:
        """
        Push files from a directory to the object store.

        :param output_prefix: The base path to calculate relative paths.
        :param output_path: The path to the directory containing files to push.
        :raises ValueError: If the output_path doesn't exist or is not a directory.
        :raises IOError: If there's an error reading files.
        :raises Exception: If there's an error with the Dapr client operations.

        Usage:
            >>> ObjectStore.push_to_object_store("logs", "/tmp/logs")
        """
        if not os.path.isdir(output_path):
            raise ValueError(
                f"The provided output_path '{output_path}' is not a valid directory."
            )

        client = DaprClient()
        try:
            for root, _, files in os.walk(output_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, "rb") as f:
                            file_content = f.read()
                    except IOError as e:
                        logger.error(f"Error reading file {file_path}: {str(e)}")
                        continue

                    relative_path = os.path.relpath(file_path, output_prefix)
                    metadata = {"key": relative_path, "fileName": relative_path}

                    try:
                        client.invoke_binding(
                            binding_name=cls.OBJECT_STORE_NAME,
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

            logger.info(f"Completed pushing data from {output_path} to object store")
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while pushing files to object store: {str(e)}"
            )
            raise e
        finally:
            client.close()
