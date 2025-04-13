"""Specific processor implementations for Atlas publish workflow."""

import glob
import os
from typing import Any, Dict

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.publish_app.models.plan import AssetTypeAssignment, FileAssignment
from application_sdk.publish_app.processors.base import BaseProcessor

logger = get_logger(__name__)


class TypeLevelProcessor(BaseProcessor):
    """Processor for standard asset types (type-level processing)."""

    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process all files for an asset type.

        Args:
            assignment: Asset type assignment to process

        Returns:
            Processing results
        """
        logger.info(f"Processing type-level assignment for {assignment.asset_type}")

        results = {
            "asset_type": assignment.asset_type,
            "diff_status": assignment.diff_status,
            "is_deletion": assignment.is_deletion,
            "total_files": 0,
            "successful_files": 0,
            "failed_files": 0,
            "total_records": 0,
            "successful_records": 0,
            "failed_records": 0,
            "details": {},
        }

        # Get parquet files from the directory
        directory = assignment.directory_prefix
        if not os.path.exists(directory):
            logger.warning(f"Directory not found: {directory}")
            return results

        # Find all parquet files in the directory
        parquet_files = glob.glob(
            os.path.join(directory, "**/*.parquet"), recursive=True
        )
        logger.info(f"Found {len(parquet_files)} parquet files in {directory}")

        results["total_files"] = len(parquet_files)

        # Process each file
        all_records = []
        for file_path in parquet_files:
            try:
                # Read records from file
                records = await self._read_parquet_file(file_path)
                all_records.extend(records)

                # Update file state as in-progress
                await self._update_state(
                    file_path, "IN_PROGRESS", {"records_found": len(records)}
                )

            except Exception as e:
                logger.error(f"Failed to read file {file_path}: {str(e)}")
                results["failed_files"] += 1

                # Update file state as failed
                await self._update_state(file_path, "FAILED", {"error": str(e)})

                # Check if we should continue
                if not self.config.continue_on_error:
                    logger.error("Stopping due to error and continue_on_error=False")
                    break

        # Process all records in chunks
        if all_records:
            logger.info(
                f"Processing {len(all_records)} records for {assignment.asset_type}"
            )
            results["total_records"] = len(all_records)

            # Process records
            process_results = await self._chunk_and_process(
                all_records, assignment.is_deletion
            )

            # Update results
            results["successful_records"] = process_results["successful"]
            results["failed_records"] = process_results["failed"]
            results["details"] = process_results

            # Update file statuses
            for file_path in parquet_files:
                if process_results["failed"] > 0:
                    if process_results["successful"] > 0:
                        status = "PARTIAL_SUCCESS"
                    else:
                        status = "FAILED"
                else:
                    status = "SUCCESS"

                await self._update_state(
                    file_path,
                    status,
                    {
                        "processed": {
                            "total": process_results["total"],
                            "successful": process_results["successful"],
                            "failed": process_results["failed"],
                        }
                    },
                )

                # Update success/failure counts for files
                if status == "SUCCESS":
                    results["successful_files"] += 1
                elif status == "PARTIAL_SUCCESS":
                    results["successful_files"] += (
                        1  # Count partial as success for files
                    )
                else:
                    results["failed_files"] += 1

        logger.info(
            f"Completed type-level processing for {assignment.asset_type}: "
            f"{results['successful_records']}/{results['total_records']} records successful"
        )

        return results


class FileLevelProcessor(BaseProcessor):
    """Processor for high-density asset types (file-level processing)."""

    async def process_file(
        self, file_assignment: FileAssignment, assignment: AssetTypeAssignment
    ) -> Dict[str, Any]:
        """Process a single file for a high-density asset type.

        Args:
            file_assignment: File assignment to process
            assignment: Parent asset type assignment

        Returns:
            Processing results for the file
        """
        file_path = file_assignment.file_path
        logger.info(f"Processing file {file_path} for {assignment.asset_type}")

        results = {
            "file_path": file_path,
            "asset_type": assignment.asset_type,
            "diff_status": assignment.diff_status,
            "is_deletion": assignment.is_deletion,
            "total_records": 0,
            "successful_records": 0,
            "failed_records": 0,
            "details": {},
        }

        try:
            # Update file state as in-progress
            await self._update_state(file_path, "IN_PROGRESS", {})

            # Read records from file
            records = await self._read_parquet_file(file_path)
            results["total_records"] = len(records)

            # Process records
            if records:
                process_results = await self._chunk_and_process(
                    records, assignment.is_deletion
                )

                # Update results
                results["successful_records"] = process_results["successful"]
                results["failed_records"] = process_results["failed"]
                results["details"] = process_results

                # Update file status
                if process_results["failed"] > 0:
                    if process_results["successful"] > 0:
                        status = "PARTIAL_SUCCESS"
                    else:
                        status = "FAILED"
                else:
                    status = "SUCCESS"

                await self._update_state(
                    file_path,
                    status,
                    {
                        "processed": {
                            "total": process_results["total"],
                            "successful": process_results["successful"],
                            "failed": process_results["failed"],
                        }
                    },
                )
            else:
                logger.warning(f"No records found in {file_path}")
                await self._update_state(
                    file_path,
                    "SUCCESS",
                    {"processed": {"total": 0, "successful": 0, "failed": 0}},
                )

            logger.info(
                f"Completed processing file {file_path}: "
                f"{results['successful_records']}/{results['total_records']} records successful"
            )

            return results

        except Exception as e:
            logger.error(f"Failed to process file {file_path}: {str(e)}")

            # Update file state as failed
            await self._update_state(file_path, "FAILED", {"error": str(e)})

            results["failed_records"] = results["total_records"]
            results["details"] = {"error": str(e)}

            return results

    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process all files for a high-density asset type.

        Args:
            assignment: Asset type assignment to process

        Returns:
            Processing results
        """
        logger.info(f"Processing file-level assignment for {assignment.asset_type}")

        results = {
            "asset_type": assignment.asset_type,
            "diff_status": assignment.diff_status,
            "is_deletion": assignment.is_deletion,
            "total_files": len(assignment.files),
            "successful_files": 0,
            "failed_files": 0,
            "total_records": 0,
            "successful_records": 0,
            "failed_records": 0,
            "file_results": [],
        }

        # Process each file individually
        for file_assignment in assignment.files:
            try:
                file_result = await self.process_file(file_assignment, assignment)
                results["file_results"].append(file_result)

                # Update aggregated counts
                results["total_records"] += file_result["total_records"]
                results["successful_records"] += file_result["successful_records"]
                results["failed_records"] += file_result["failed_records"]

                # Update file success/failure
                if file_result["failed_records"] > 0:
                    if file_result["successful_records"] > 0:
                        # Partial success, count as success
                        results["successful_files"] += 1
                    else:
                        results["failed_files"] += 1
                else:
                    results["successful_files"] += 1

            except Exception as e:
                logger.error(f"Failed to process file assignment: {str(e)}")
                results["failed_files"] += 1

                # Check if we should continue
                if not self.config.continue_on_error:
                    logger.error("Stopping due to error and continue_on_error=False")
                    break

        logger.info(
            f"Completed file-level processing for {assignment.asset_type}: "
            f"{results['successful_files']}/{results['total_files']} files successful, "
            f"{results['successful_records']}/{results['total_records']} records successful"
        )

        return results


class SelfRefProcessor(BaseProcessor):
    """Processor for self-referential types with two-pass approach."""

    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process a self-referential asset type with two passes.

        First pass: Create entities without relationship attributes
        Second pass: Update entities with only relationship attributes

        Args:
            assignment: Asset type assignment to process

        Returns:
            Processing results
        """
        logger.info(
            f"Processing self-referential type {assignment.asset_type} with two-pass approach"
        )

        results = {
            "asset_type": assignment.asset_type,
            "diff_status": assignment.diff_status,
            "is_deletion": assignment.is_deletion,
            "total_files": 0,
            "successful_files": 0,
            "failed_files": 0,
            "total_records": 0,
            "first_pass_successful": 0,
            "second_pass_successful": 0,
            "failed_records": 0,
            "first_pass_results": {},
            "second_pass_results": {},
        }

        # If this is a deletion, we can use the standard type-level processor
        if assignment.is_deletion:
            logger.info(
                f"Self-referential type {assignment.asset_type} is for deletion, using standard processor"
            )
            standard_processor = TypeLevelProcessor(
                self.client, self.config, self.state_store
            )
            standard_results = await standard_processor.process(assignment)

            # Copy results
            results.update(standard_results)
            return results

        # Get parquet files from the directory
        directory = assignment.directory_prefix
        if not os.path.exists(directory):
            logger.warning(f"Directory not found: {directory}")
            return results

        # Find all parquet files in the directory
        parquet_files = glob.glob(
            os.path.join(directory, "**/*.parquet"), recursive=True
        )
        logger.info(f"Found {len(parquet_files)} parquet files in {directory}")

        results["total_files"] = len(parquet_files)

        # Process each file
        all_records = []
        for file_path in parquet_files:
            try:
                # Read records from file
                records = await self._read_parquet_file(file_path)
                all_records.extend(records)

                # Update file state as in-progress
                await self._update_state(
                    file_path, "IN_PROGRESS", {"records_found": len(records)}
                )

            except Exception as e:
                logger.error(f"Failed to read file {file_path}: {str(e)}")
                results["failed_files"] += 1

                # Update file state as failed
                await self._update_state(file_path, "FAILED", {"error": str(e)})

                # Check if we should continue
                if not self.config.continue_on_error:
                    logger.error("Stopping due to error and continue_on_error=False")
                    break

        # Process all records in two passes
        if all_records:
            results["total_records"] = len(all_records)

            # First pass: Create entities without relationship attributes
            logger.info(
                f"First pass: Creating {len(all_records)} entities without relationships"
            )
            first_pass_entities = []

            for record in all_records:
                try:
                    # Parse the record
                    # entity_json = record.get("record", "{}")

                    # FIXME(inishchith): Implement actual relationship stripping logic based on entity structure
                    # For now, we'll just pass the entity as-is
                    first_pass_entities.append(record)
                except Exception as e:
                    logger.error(f"Failed to prepare first-pass entity: {str(e)}")

            # Process first pass
            first_pass_results = await self._chunk_and_process(
                first_pass_entities, False
            )
            results["first_pass_results"] = first_pass_results
            results["first_pass_successful"] = first_pass_results["successful"]

            # Check if we should continue to second pass
            if first_pass_results["successful"] > 0:
                # Second pass: Update entities with only relationship attributes
                logger.info(
                    f"Second pass: Updating {first_pass_results['successful']} entities with relationships"
                )

                # Get successful entities from first pass
                successful_qualified_names = set()

                for detail in first_pass_results.get("details", []):
                    if detail.get("status") == "SUCCESS":
                        qualified_name = detail.get("qualified_name")
                        if qualified_name:
                            successful_qualified_names.add(qualified_name)

                # Get original records for successful entities
                second_pass_entities = []
                for record in all_records:
                    qualified_name = record.get("qualified_name")
                    if qualified_name in successful_qualified_names:
                        # FIXME(inishchith): Implement relationship extraction logic
                        # For now, we'll just pass the entity as-is
                        second_pass_entities.append(record)

                # Process second pass
                if second_pass_entities:
                    second_pass_results = await self._chunk_and_process(
                        second_pass_entities, False
                    )
                    results["second_pass_results"] = second_pass_results
                    results["second_pass_successful"] = second_pass_results[
                        "successful"
                    ]

                    # Calculate overall success/failure
                    results["successful_records"] = second_pass_results["successful"]
                    results["failed_records"] = (
                        results["total_records"] - results["successful_records"]
                    )
                else:
                    logger.warning("No entities for second pass")
                    results["successful_records"] = first_pass_results["successful"]
                    results["failed_records"] = (
                        results["total_records"] - results["successful_records"]
                    )
            else:
                logger.warning(
                    "No successful entities from first pass, skipping second pass"
                )
                results["successful_records"] = 0
                results["failed_records"] = results["total_records"]

            # Update file statuses
            for file_path in parquet_files:
                if results["failed_records"] > 0:
                    if results["successful_records"] > 0:
                        status = "PARTIAL_SUCCESS"
                    else:
                        status = "FAILED"
                else:
                    status = "SUCCESS"

                await self._update_state(
                    file_path,
                    status,
                    {
                        "processed": {
                            "total": results["total_records"],
                            "successful": results["successful_records"],
                            "failed": results["failed_records"],
                            "first_pass_successful": results["first_pass_successful"],
                            "second_pass_successful": results["second_pass_successful"],
                        }
                    },
                )

                # Update success/failure counts for files
                if status == "SUCCESS":
                    results["successful_files"] += 1
                elif status == "PARTIAL_SUCCESS":
                    results["successful_files"] += (
                        1  # Count partial as success for files
                    )
                else:
                    results["failed_files"] += 1

        logger.info(
            f"Completed two-pass processing for {assignment.asset_type}: "
            f"{results['successful_records']}/{results['total_records']} records successful"
        )

        return results
