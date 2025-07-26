"""Atlan storage interface for upload operations and migration from objectstore."""

from typing import Any, Dict, List

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.constants import (
    DEPLOYMENT_OBJECT_STORE_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


# keeping any logic related to operations on atlan storage within this file.
class AtlanStorageOutput:
    """Handles upload operations to Atlan storage and migration from objectstore."""

    OBJECT_CREATE_OPERATION = "create"

    @classmethod
    def upload_file(cls, file_path: str, file_data: bytes) -> None:
        """
        Upload file data to Atlan storage.

        Args:
            file_path (str): The path where the file should be stored in Atlan storage
            file_data (bytes): The raw file data to upload

        Raises:
            Exception: If there's an error uploading the file to Atlan storage.
        """
        try:
            with DaprClient() as client:
                metadata = {"key": file_path, "fileName": file_path}

                client.invoke_binding(
                    binding_name=UPSTREAM_OBJECT_STORE_NAME,
                    operation=cls.OBJECT_CREATE_OPERATION,
                    data=file_data,
                    binding_metadata=metadata,
                )

                logger.debug(
                    f"Successfully uploaded file to Atlan storage: {file_path}"
                )

        except Exception as e:
            logger.error(f"Error uploading file {file_path} to Atlan storage: {str(e)}")
            raise e

    @classmethod
    async def migrate_from_objectstore_to_atlan(
        cls, prefix: str = ""
    ) -> Dict[str, Any]:
        """
        Migrate all files from objectstore to Atlan storage under a given prefix.

        Args:
            prefix (str): The prefix to filter which files to migrate. Empty string migrates all files.

        Returns:
            dict: Migration summary with counts and any failures
        """
        try:
            logger.info(
                f"Starting migration from objectstore to Atlan storage with prefix: '{prefix}'"
            )

            # Get list of all files to migrate from objectstore
            files_to_migrate = ObjectStoreInput.list_all_files(
                prefix, object_store_name=DEPLOYMENT_OBJECT_STORE_NAME
            )

            total_files = len(files_to_migrate)
            logger.info(f"Found {total_files} files to migrate")

            if total_files == 0:
                logger.info("No files found to migrate")
                return {
                    "total_files": 0,
                    "migrated_files": 0,
                    "failed_migrations": 0,
                    "failures": [],
                    "prefix": prefix,
                    "source": "objectstore",
                    "destination": UPSTREAM_OBJECT_STORE_NAME,
                }

            if total_files > 0:
                logger.info(
                    f"Files to migrate: {files_to_migrate[:5]}..."
                )  # Log first 5 files

            # Migrate each file
            migrated_count = 0
            failed_migrations: List[Dict[str, str]] = []

            for i, file_path in enumerate(files_to_migrate, 1):
                if i % 100 == 0 or i == total_files:  # Log progress every 100 files
                    logger.info(f"Migrating file {i}/{total_files}: {file_path}")

                try:
                    # Get file data from objectstore
                    file_data = ObjectStoreInput.get_file_data(
                        file_path, object_store_name=DEPLOYMENT_OBJECT_STORE_NAME
                    )

                    # Upload to Atlan storage
                    cls.upload_file(file_path, file_data)

                    migrated_count += 1
                    logger.debug(f"Successfully migrated: {file_path}")

                except Exception as e:
                    logger.error(f"Failed to migrate file {file_path}: {str(e)}")
                    failed_migrations.append({"file": file_path, "error": str(e)})

            migration_summary = {
                "total_files": total_files,
                "migrated_files": migrated_count,
                "failed_migrations": len(failed_migrations),
                "failures": failed_migrations,
                "prefix": prefix,
                "source": "objectstore",
                "destination": UPSTREAM_OBJECT_STORE_NAME,
            }

            logger.info(f"Migration completed: {migration_summary}")
            return migration_summary

        except Exception as e:
            logger.error(f"Migration failed for prefix '{prefix}': {str(e)}")
            raise e
