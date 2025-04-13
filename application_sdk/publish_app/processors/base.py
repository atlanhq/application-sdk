"""Base processor for Atlas publish workflow."""

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import daft

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.publish_app.client.atlas_client import AtlasClient
from application_sdk.publish_app.models.plan import AssetTypeAssignment, PublishConfig

logger = get_logger(__name__)


class BaseProcessor(ABC):
    """Base class for asset processors with common functionality."""

    def __init__(
        self, atlas_client: AtlasClient, config: PublishConfig, state_store: Any
    ):
        """Initialize with client, config and state store.

        Args:
            atlas_client: Atlas API client
            config: Publish configuration
            state_store: DAPR state store client
        """
        self.client = atlas_client
        self.config = config
        self.state_store = state_store

    @abstractmethod
    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process an asset type assignment.

        Args:
            assignment: Asset type assignment to process

        Returns:
            Processing results
        """
        pass

    async def _read_parquet_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Read a parquet file into a list of records.

        Args:
            file_path: Path to the parquet file

        Returns:
            List of records
        """
        try:
            logger.debug(f"Reading parquet file: {file_path}")
            df = daft.read_parquet(file_path)

            # Convert to Python dictionaries
            records = df.to_pandas().to_dict("records")
            logger.debug(f"Read {len(records)} records from {file_path}")

            return records
        except Exception as e:
            logger.error(f"Failed to read parquet file {file_path}: {str(e)}")
            raise

    async def _transform_records(
        self, records: List[Dict[str, Any]], is_deletion: bool
    ) -> List[Dict[str, Any]]:
        """Transform records into Atlas API format.

        Args:
            records: List of records to transform
            is_deletion: Whether these are deletion records

        Returns:
            Transformed records ready for Atlas API
        """
        transformed_records = []

        for record in records:
            if is_deletion:
                # For deletion, we only need the qualified_name
                transformed = {
                    "typeName": record.get("type_name"),
                    "uniqueAttributes": {"qualifiedName": record.get("qualified_name")},
                }
            else:
                # For create/update, we need the full record
                try:
                    # Parse the JSON string in the record field
                    atlas_entity = json.loads(record.get("record", "{}"))
                    transformed = atlas_entity
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(f"Failed to parse record JSON: {str(e)}")
                    continue

            transformed_records.append(transformed)

        return transformed_records

    async def _chunk_and_process(
        self, records: List[Dict[str, Any]], is_deletion: bool
    ) -> Dict[str, Any]:
        """Chunk records and process in batches.

        Args:
            records: List of records to process
            is_deletion: Whether these are deletion records

        Returns:
            Processing results
        """
        results = {"total": len(records), "successful": 0, "failed": 0, "details": []}

        if not records:
            logger.info("No records to process")
            return results

        # Transform records for Atlas API
        transformed_records = await self._transform_records(records, is_deletion)

        # Process in chunks
        chunk_size = self.config.publish_chunk_count
        for i in range(0, len(transformed_records), chunk_size):
            chunk = transformed_records[i : i + chunk_size]
            original_chunk = records[i : i + chunk_size]

            logger.info(
                f"Processing chunk {i//chunk_size + 1}/{(len(transformed_records) + chunk_size - 1)//chunk_size} with {len(chunk)} records"
            )

            try:
                # Process based on operation type
                if is_deletion:
                    # Extract GUIDs from uniqueAttributes
                    guids = [
                        record.get("guid") for record in chunk if record.get("guid")
                    ]
                    if guids:
                        response = await self.client.delete_entities(guids)
                    else:
                        logger.warning("No GUIDs found for deletion")
                        response = {"mutatedEntities": {"DELETE": []}}
                else:
                    # Create or update
                    response = await self.client.create_update_entities(chunk)

                # Process response
                chunk_result = self._process_response(
                    response, original_chunk, is_deletion
                )

                # Update counts
                results["successful"] += chunk_result["successful"]
                results["failed"] += chunk_result["failed"]
                results["details"].extend(chunk_result["details"])

                # Update state
                await self._update_chunk_state(original_chunk, chunk_result)

            except Exception as e:
                logger.error(f"Failed to process chunk: {str(e)}")

                # Mark all as failed
                failed_details = []
                for record in original_chunk:
                    detail = {
                        "qualified_name": record.get("qualified_name"),
                        "status": "FAILED",
                        "error_code": "PROCESSING_ERROR",
                        "error_description": str(e),
                        "request_config": {
                            "api_endpoint": "/api/atlas/v2/entity/bulk",
                            "method": "DELETE" if is_deletion else "POST",
                            "retry_count": 0,
                        },
                        "runbook_link": None,
                    }
                    failed_details.append(detail)

                results["failed"] += len(original_chunk)
                results["details"].extend(failed_details)

                # Update state for failures
                await self._update_chunk_state(
                    original_chunk, {"details": failed_details}
                )

                # Check if we should continue
                if not self.config.continue_on_error:
                    logger.error("Stopping due to error and continue_on_error=False")
                    break

        return results

    def _process_response(
        self,
        response: Dict[str, Any],
        original_records: List[Dict[str, Any]],
        is_deletion: bool,
    ) -> Dict[str, Any]:
        """Process API response and generate result details.

        Args:
            response: API response
            original_records: Original records being processed
            is_deletion: Whether these are deletion records

        Returns:
            Processed results with status for each record
        """
        result = {"successful": 0, "failed": 0, "details": []}

        # Map qualified names to original records
        record_map = {
            record.get("qualified_name"): record for record in original_records
        }

        # Get entity results from response
        if is_deletion:
            entities = response.get("mutatedEntities", {}).get("DELETE", [])
            operation = "DELETE"
            api_endpoint = "/api/atlas/v2/entity/bulk"
        else:
            entities = response.get("mutatedEntities", {}).get("CREATE", [])
            entities.extend(response.get("mutatedEntities", {}).get("UPDATE", []))
            operation = "CREATE/UPDATE"
            api_endpoint = "/api/atlas/v2/entity/bulk"

        # Process successful entities
        for entity in entities:
            qualified_name = entity.get("attributes", {}).get("qualifiedName")
            if qualified_name and qualified_name in record_map:
                result["successful"] += 1

                detail = {
                    "qualified_name": qualified_name,
                    "status": "SUCCESS",
                    "error_code": None,
                    "atlas_error_code": None,
                    "error_description": None,
                    "request_config": {
                        "api_endpoint": api_endpoint,
                        "method": operation,
                        "retry_count": 0,  # FIXME: Track actual retry count
                    },
                    "runbook_link": None,
                }

                result["details"].append(detail)

        # Process failures - any records not in the successful list
        for qualified_name, record in record_map.items():
            if not any(
                detail["qualified_name"] == qualified_name
                for detail in result["details"]
            ):
                result["failed"] += 1

                detail = {
                    "qualified_name": qualified_name,
                    "status": "FAILED",
                    "error_code": "ENTITY_NOT_PROCESSED",
                    "atlas_error_code": None,
                    "error_description": "Entity was not processed by Atlas",
                    "request_config": {
                        "api_endpoint": api_endpoint,
                        "method": operation,
                        "retry_count": 0,  # FIXME: Track actual retry count
                    },
                    "runbook_link": None,
                }

                result["details"].append(detail)

        return result

    async def _update_chunk_state(
        self, original_records: List[Dict[str, Any]], result: Dict[str, Any]
    ) -> None:
        """Update state in DAPR store for processed records.

        Args:
            original_records: Original records that were processed
            result: Processing results
        """
        # Skip if there's no state store
        if not self.state_store:
            logger.warning("No state store available, skipping state update")
            return

        # Map details by qualified name
        detail_map = {
            detail["qualified_name"]: detail for detail in result.get("details", [])
        }

        # Update each record in the state store
        for record in original_records:
            qualified_name = record.get("qualified_name")
            if not qualified_name:
                continue

            # Get result detail for this record
            detail = detail_map.get(
                qualified_name,
                {
                    "status": "UNKNOWN",
                    "error_code": None,
                    "atlas_error_code": None,
                    "error_description": "No status information available",
                    "request_config": {
                        "api_endpoint": "unknown",
                        "method": "unknown",
                        "retry_count": 0,
                    },
                    "runbook_link": None,
                },
            )

            # Create state entry
            state_key = f"atlas_publish_{qualified_name}"
            state_value = {
                "qualified_name": qualified_name,
                "type_name": record.get("type_name"),
                "timestamp": int(time.time() * 1000),
                "status": detail.get("status"),
                "error_code": detail.get("error_code"),
                "atlas_error_code": detail.get("atlas_error_code"),
                "error_description": detail.get("error_description"),
                "request_config": detail.get("request_config"),
                "runbook_link": detail.get("runbook_link"),
            }

            try:
                # Save to state store
                await self.state_store.save_state(state_key, state_value)
            except Exception as e:
                logger.error(f"Failed to update state for {qualified_name}: {str(e)}")

    async def _update_state(
        self, file_path: str, status: str, details: Dict[str, Any]
    ) -> None:
        """Update state in DAPR store for a file.

        Args:
            file_path: Path to the file
            status: Processing status (SUCCESS, FAILED, PARTIAL_SUCCESS)
            details: Processing details
        """
        if not self.state_store:
            logger.warning("No state store available, skipping state update")
            return

        try:
            state_key = f"atlas_publish_file_{file_path.replace('/', '_')}"
            state_value = {
                "file_path": file_path,
                "status": status,
                "timestamp": int(time.time() * 1000),
                "details": details,
            }

            await self.state_store.save_state(state_key, state_value)
        except Exception as e:
            logger.error(f"Failed to update state for file {file_path}: {str(e)}")
