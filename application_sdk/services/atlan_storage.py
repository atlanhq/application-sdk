"""Atlan storage service for upload operations and migration from object store.

This module provides the AtlanStorage service for handling data migration between
local object storage and Atlan's upstream storage system. It's specifically designed
for the bucket cloning strategy used in customer-deployed applications.

The service supports parallel file migration with comprehensive error handling and
detailed reporting through the MigrationSummary model.
"""

import asyncio
from typing import Dict, List

from dapr.clients import DaprClient
from pydantic import BaseModel
from temporalio import activity

from application_sdk.constants import (
    DEPLOYMENT_OBJECT_STORE_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

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


class AtlanStorage:
    """Handles upload operations to Atlan storage and migration from objectstore."""

    OBJECT_CREATE_OPERATION = "create"

    @classmethod
    async def _migrate_single_file(cls, file_path: str) -> tuple[str, bool, str]:
        """Migrate a single file from object store to Atlan storage.

        This internal method handles the migration of a single file, including
        error handling and logging. It's designed to be called concurrently
        for multiple files.

        Args:
            file_path (str): The path of the file to migrate in the object store.

        Returns:
            tuple[str, bool, str]: A tuple containing:
                - file_path: The path of the file that was processed
                - success: Boolean indicating if migration was successful
                - error_message: Error details if migration failed, empty string if successful

        Note:
            This method is internal and should not be called directly. Use
            migrate_from_objectstore_to_atlan() instead for proper coordination
            and error handling.
        """
        try:
            # Get file data from objectstore
            file_data = await ObjectStore.get_content(
                file_path, store_name=DEPLOYMENT_OBJECT_STORE_NAME
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
        """Migrate all files from object store to Atlan storage under a given prefix.

        This method performs a parallel migration of files from the local object store
        to Atlan's upstream storage system. It provides comprehensive error handling
        and detailed reporting of the migration process.

        Args:
            prefix (str, optional): The prefix to filter which files to migrate.
                Empty string migrates all files. Defaults to "".

        Returns:
            MigrationSummary: Comprehensive migration summary including:
                - total_files: Number of files found for migration
                - migrated_files: Number successfully migrated
                - failed_migrations: Number that failed to migrate
                - failures: List of failure details with file paths and errors
                - prefix: The prefix used for filtering
                - source/destination: Storage system identifiers

        Raises:
            Exception: If there's a critical error during the migration process.

        Examples:
            >>> # Migrate all files
            >>> summary = await AtlanStorage.migrate_from_objectstore_to_atlan()
            >>> print(f"Success rate: {summary.migrated_files/summary.total_files*100:.1f}%")

            >>> # Migrate specific dataset
            >>> summary = await AtlanStorage.migrate_from_objectstore_to_atlan(
            ...     prefix="processed_data/2024/"
            ... )
            >>> if summary.total_files == 0:
            ...     print("No files found with the specified prefix")
            >>> elif summary.failed_migrations == 0:
            ...     print(f"Successfully migrated all {summary.total_files} files")
            >>> else:
            ...     print(f"Migration completed with {summary.failed_migrations} failures")
            ...     # Handle failures...
        """
        try:
            logger.info(
                f"Starting migration from objectstore to Atlan storage with prefix: '{prefix}'"
            )

            # Get list of all files to migrate from objectstore
            files_to_migrate = await ObjectStore.list_files(
                prefix, store_name=DEPLOYMENT_OBJECT_STORE_NAME
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
