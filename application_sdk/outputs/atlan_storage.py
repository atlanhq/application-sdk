"""Atlan storage interface for upload operations and migration from objectstore."""

import asyncio
from typing import Dict, List

from dapr.clients import DaprClient
from pydantic import BaseModel
from temporalio import activity

from application_sdk.constants import (
    DEPLOYMENT_OBJECT_STORE_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class MigrationSummary(BaseModel):
    """Summary of migration operation from objectstore to Atlan storage.

    This model tracks the results of migrating files from objectstore to Atlan storage,
    including success/failure counts and detailed error information.

    Attributes:
        total_files: Total number of files found for migration.
        migrated_files: Number of files successfully migrated.
        failed_migrations: Number of files that failed to migrate.
        failures: List of failure details with file paths and error messages.
        prefix: The prefix used to filter files for migration.
        source: Source storage system (e.g., "objectstore").
        destination: Destination storage system (e.g., "upstream-objectstore").
    """

    total_files: int = 0
    migrated_files: int = 0
    failed_migrations: int = 0
    failures: List[Dict[str, str]] = []
    prefix: str = ""
    source: str = DEPLOYMENT_OBJECT_STORE_NAME
    destination: str = UPSTREAM_OBJECT_STORE_NAME


# keeping any logic related to operations on atlan storage within this file.
class AtlanStorageOutput:
    """Handles upload operations to Atlan storage and migration from objectstore."""

    OBJECT_CREATE_OPERATION = "create"

    @classmethod
    async def _migrate_single_file(cls, file_path: str) -> tuple[str, bool, str]:
        """
        Migrate a single file from objectstore to Atlan storage.

        Args:
            file_path (str): The path of the file to migrate

        Returns:
            tuple[str, bool, str]: (file_path, success, error_message)
        """
        try:
            # Get file data from objectstore
            file_data = ObjectStoreInput.get_file_data(
                file_path, object_store_name=DEPLOYMENT_OBJECT_STORE_NAME
            )

            with DaprClient() as client:
                metadata = {"key": file_path}

                client.invoke_binding(
                    binding_name=UPSTREAM_OBJECT_STORE_NAME,
                    operation=cls.OBJECT_CREATE_OPERATION,
                    data=file_data,
                    binding_metadata=metadata,
                )

                logger.debug(
                    f"Successfully uploaded file to Atlan storage: {file_path}"
                )

            logger.debug(f"Successfully migrated: {file_path}")
            return file_path, True, ""
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to migrate file {file_path}: {error_msg}")
            return file_path, False, error_msg

    @classmethod
    async def migrate_from_objectstore_to_atlan(
        cls, prefix: str = ""
    ) -> MigrationSummary:
        """
        Migrate all files from objectstore to Atlan storage under a given prefix.

        Args:
            prefix (str): The prefix to filter which files to migrate. Empty string migrates all files.

        Returns:
            MigrationSummary: Migration summary with counts and any failures
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
                return MigrationSummary(
                    prefix=prefix,
                    destination=UPSTREAM_OBJECT_STORE_NAME,
                )

            # Create migration tasks for all files
            migration_tasks = [
                asyncio.create_task(cls._migrate_single_file(file_path))
                for file_path in files_to_migrate
            ]

            # Execute all migrations in parallel
            logger.info(f"Starting parallel migration of {total_files} files")
            results = await asyncio.gather(*migration_tasks, return_exceptions=True)

            # Process results
            migrated_count = 0
            failed_migrations: List[Dict[str, str]] = []

            for result in results:
                if isinstance(result, Exception):
                    # Handle unexpected exceptions
                    logger.error(f"Unexpected error during migration: {str(result)}")
                    failed_migrations.append({"file": "unknown", "error": str(result)})
                else:
                    file_path, success, error_msg = result
                    if success:
                        migrated_count += 1
                    else:
                        failed_migrations.append(
                            {"file": file_path, "error": error_msg}
                        )

            migration_summary = MigrationSummary(
                total_files=total_files,
                migrated_files=migrated_count,
                failed_migrations=len(failed_migrations),
                failures=failed_migrations,
                prefix=prefix,
                destination=UPSTREAM_OBJECT_STORE_NAME,
            )

            logger.info(f"Migration completed: {migration_summary}")
            return migration_summary

        except Exception as e:
            logger.error(f"Migration failed for prefix '{prefix}': {str(e)}")
            raise e
