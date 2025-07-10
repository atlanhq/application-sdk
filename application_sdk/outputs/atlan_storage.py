"""Atlan storage interface for upload operations and migration from objectstore."""

from typing import Any, Dict, List, Optional

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class AtlanStorageOutput:
    """Handles upload operations to Atlan storage and migration from objectstore."""

    ATLAN_STORE_NAME = "atlan-storage"  # The S3 binding name
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
                    binding_name=cls.ATLAN_STORE_NAME,
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
    async def _migrate_files_internal(
        cls, files_to_migrate: List[str], prefix: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Internal method to migrate a list of files from objectstore to Atlan storage.

        Args:
            files_to_migrate (List[str]): List of file paths to migrate
            prefix (Optional[str]): Optional prefix for logging context

        Returns:
            dict: Migration summary with counts and any failures
        """
        migrated_count = 0
        failed_migrations: List[Dict[str, str]] = []
        total_files = len(files_to_migrate)

        if total_files == 0:
            logger.info("No files found to migrate")
            return {
                "total_files": 0,
                "migrated_files": 0,
                "failed_migrations": 0,
                "failures": [],
                "prefix": prefix,
            }

        # Migrate each file
        for i, file_path in enumerate(files_to_migrate, 1):
            if i % 100 == 0 or i == total_files:  # Log progress every 100 files
                logger.info(f"Migrating file {i}/{total_files}: {file_path}")

            try:
                # Get file data from objectstore
                file_data = ObjectStoreInput.get_file_data(file_path)

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
            "destination": cls.ATLAN_STORE_NAME,
        }

        logger.info(f"Migration completed: {migration_summary}")
        return migration_summary

    @classmethod
    async def migrate_from_objectstore(cls, prefix: str = "") -> Dict[str, Any]:
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
            files_to_migrate = ObjectStoreInput.list_all_files(prefix)

            total_files = len(files_to_migrate)
            logger.info(f"Found {total_files} files to migrate")

            if total_files > 0:
                logger.info(
                    f"Files to migrate: {files_to_migrate[:5]}..."
                )  # Log first 5 files

            return await cls._migrate_files_internal(files_to_migrate, prefix)

        except Exception as e:
            logger.error(f"Migration failed for prefix '{prefix}': {str(e)}")
            raise e

    @classmethod
    def migrate_specific_files(cls, file_paths: List[str]) -> Dict[str, Any]:
        """
        Migrate only specific files from objectstore to Atlan storage.

        Args:
            file_paths (List[str]): List of specific file paths to migrate

        Returns:
            dict: Migration summary
        """
        try:
            logger.info(
                f"Starting migration of {len(file_paths)} specific files to Atlan storage"
            )

            # Use the internal method to avoid code duplication
            import asyncio

            return asyncio.run(cls._migrate_files_internal(file_paths))

        except Exception as e:
            logger.error(f"Specific file migration failed: {str(e)}")
            raise e

    @classmethod
    async def migrate_prefix_batch(cls, prefixes: List[str]) -> Dict[str, Any]:
        """
        Migrate multiple prefixes in batch from objectstore to Atlan storage.

        Args:
            prefixes (List[str]): List of prefixes to migrate

        Returns:
            dict: Combined migration summary for all prefixes
        """
        total_migrated = 0
        total_files = 0
        all_failures: List[Dict[str, str]] = []

        try:
            logger.info(f"Starting batch migration of {len(prefixes)} prefixes")

            for i, prefix in enumerate(prefixes, 1):
                logger.info(f"Migrating prefix {i}/{len(prefixes)}: '{prefix}'")

                try:
                    result = await cls.migrate_from_objectstore(prefix)
                    total_files += result["total_files"]
                    total_migrated += result["migrated_files"]
                    all_failures.extend(result["failures"])

                except Exception as e:
                    logger.error(f"Failed to migrate prefix '{prefix}': {str(e)}")
                    all_failures.append({"prefix": prefix, "error": str(e)})

            batch_summary = {
                "total_prefixes": len(prefixes),
                "total_files": total_files,
                "migrated_files": total_migrated,
                "failed_migrations": len(all_failures),
                "failures": all_failures,
                "source": "objectstore",
                "destination": cls.ATLAN_STORE_NAME,
            }

            logger.info(f"Batch migration completed: {batch_summary}")
            return batch_summary

        except Exception as e:
            logger.error(f"Batch migration failed: {str(e)}")
            raise e
