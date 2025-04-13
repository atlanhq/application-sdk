import asyncio
from typing import Any, Dict, List, Optional

from temporalio import activity

from application_sdk.activities import (
    ActivitiesInterface,
    ActivitiesState,
    get_workflow_id,
)
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.semaphore import InMemorySemaphore
from application_sdk.publish_app.client.atlas_client import AtlasClient
from application_sdk.publish_app.models.discovery import (
    analyze_dependencies,
    discover_directories,
)
from application_sdk.publish_app.models.plan import (
    AssetTypeAssignment,
    DiffStatus,
    FileAssignment,
    ProcessingStrategy,
    PublishConfig,
    PublishPlan,
)
from application_sdk.publish_app.processors.specific import (
    FileLevelProcessor,
    SelfRefProcessor,
    TypeLevelProcessor,
)

logger = get_logger(__name__)


class AtlasPublishAtlanActivitiesState(ActivitiesState):
    """State for Atlas publish activities."""

    access_token: Optional[str] = None


class AtlasPublishAtlanActivities(ActivitiesInterface):
    """Activities for Atlas publish workflow.

    This class provides activities for the Atlas publish workflow, including:
    - plan: Plan the publish operation based on input data
    - publish: Execute the publish plan
    """

    def __init__(self):
        """Initialize the activities."""
        super().__init__()
        self.state = AtlasPublishAtlanActivitiesState()

    @activity.defn
    async def setup_connection_entity(self, connection_args: Dict[str, Any]):
        """
        Setup the connection entity for the Atlas publish workflow.

        TODO: this is a temporary activity to setup the connection entity for the Atlas publish workflow.
        Plan is to setup connection entity and policies for the connection entity.
        """
        atlas_client = AtlasClient()
        await atlas_client.initialize()

        # FIXME(inishchith): this is temporary code to create a connection entity
        # will be updated with a variable ex: via workflow args
        await atlas_client.create_update_entities(
            [
                {
                    "attributes": {
                        "name": "nishchith-dummy",
                        "qualifiedName": "default/postgres/1688410509",
                        "allowQuery": True,
                        "allowQueryPreview": True,
                        "rowLimit": 10000,
                        "defaultCredentialGuid": "13d1c09d-8bf6-4f86-ba82-c6337f8e0616",
                        "connectorName": "postgres",
                        "sourceLogo": "https://www.postgresql.org/media/img/about/press/elephant.png",
                        "isDiscoverable": True,
                        "isEditable": False,
                        "category": "database",
                        "adminUsers": ["nishchith"],
                        "adminGroups": [],
                        "adminRoles": ["43ebdc48-bba0-4b3c-b7d0-1a9e4c1de067"],
                    },
                    "typeName": "Connection",
                }
            ]
        )

        await atlas_client.close()

        # FIXME(inishchith): await for 20 seconds for policy sync to occur
        await asyncio.sleep(20)

        logger.info(
            f"Setting up connection entity for Atlas publish workflow: {connection_args}"
        )

    @activity.defn
    async def plan(self, plan_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Plan activity for Atlas publish workflow.

        Args:
            plan_args: Dictionary containing planning arguments including:
                - input_data_path: Path to input data
                - config: PublishConfig or dict with configuration parameters

        Returns:
            Dictionary representation of PublishPlan.

        Raises:
            ValueError: If the input data path is invalid.
            Exception: If an error occurs during plan generation.
        """
        logger.info("Planning to publish to Atlas")

        # FIXME(inishchith): bad default value for testing purpose, will be removed later
        input_data_path = plan_args.get("input_data_path", "diff")

        config_data = plan_args.get("config", {})

        # Convert config to PublishConfig if it's a dictionary
        if isinstance(config_data, dict):
            config = PublishConfig(**config_data)
        else:
            config = config_data

        try:
            logger.info(f"Discovering directories in {input_data_path}")

            # Discover directories and files
            discovered_data = discover_directories(input_data_path)

            if not discovered_data:
                logger.warning(f"No data found in {input_data_path}")
                return {"error": f"No data found in {input_data_path}"}

            # Get all asset types from discovered data
            asset_types = list(discovered_data.keys())
            logger.info(f"Found asset types: {', '.join(asset_types)}")

            # Analyze dependencies and determine publish order
            (
                dependency_graph,
                self_referential_types,
                publish_order,
            ) = await analyze_dependencies(asset_types)

            logger.info(f"Publish order: {publish_order}")
            if self_referential_types:
                logger.info(
                    f"Self-referential types: {', '.join(self_referential_types)}"
                )

            # Create a dictionary to store assignments by type and diff status
            assignments_by_type = {}

            for asset_type, diff_statuses in discovered_data.items():
                assignments_by_type[asset_type] = {}

                for diff_status, info in diff_statuses.items():
                    # Determine processing strategy based on config and record count
                    processing_strategy = ProcessingStrategy.TYPE_LEVEL

                    if asset_type in config.high_density_asset_types:
                        processing_strategy = ProcessingStrategy.FILE_LEVEL

                    # Create asset type assignment
                    assignment = AssetTypeAssignment(
                        asset_type=asset_type,
                        directory_prefix=info["directory_prefix"],
                        estimated_file_count=info["estimated_file_count"],
                        estimated_total_records=info["estimated_total_records"],
                        is_deletion=(diff_status == DiffStatus.DELETE),
                        processing_strategy=processing_strategy,
                        diff_status=diff_status,
                    )

                    # For file-level processing, add individual file assignments
                    if processing_strategy == ProcessingStrategy.FILE_LEVEL:
                        for file_info in info.get("files", []):
                            assignment.files.append(
                                FileAssignment(
                                    file_path=file_info["file_path"],
                                    estimated_records=file_info["estimated_records"],
                                )
                            )

                    if diff_status not in assignments_by_type[asset_type]:
                        assignments_by_type[asset_type][diff_status] = []
                    assignments_by_type[asset_type][diff_status].append(assignment)

            # Create the publish plan
            plan = PublishPlan(
                publish_order=publish_order,
                dependency_graph=dependency_graph,
                asset_type_assignments=assignments_by_type,
                self_referential_types=self_referential_types,
            )

            logger.info(
                f"Generated publish plan with {len(assignments_by_type)} assignments"
            )

            return plan.to_dict()

        except Exception as e:
            logger.error(f"Error generating publish plan: {str(e)}")
            raise e

    @activity.defn
    async def publish(self, publish_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Publish assets to Atlas based on the plan.

        This is the main execute phase activity that processes the plan generated
        in the plan phase. It handles:
        - Processing asset types in the order specified by the plan
        - Creating/updating entities first, then deleting entities
        - Handling high-density types with file-level parallelism
        - Special processing for self-referential types

        Args:
            publish_args: Dictionary containing publish arguments, including:
                - plan: The plan output from the plan phase (required)
                - config: PublishConfig or dict with configuration parameters (required)
                - state_store: DAPR state store client (optional)

        Returns:
            Dictionary with execution results

        Raises:
            ValueError: If required arguments are missing
            Exception: If an error occurs during execution
        """

        # FIXME(inishchith): this could be at the system level (like: atlan-publish-semaphore)
        # Get workflow ID for activity concurrency control
        workflow_id = get_workflow_id() or "atlas_publish"
        semaphore_key = f"atlas_publish_{workflow_id}"

        try:
            # Initialize Atlas client
            atlas_client = AtlasClient(
                PublishConfig(username=publish_args.get("username", ""))
            )
            await atlas_client.initialize()

            assignments = [
                AssetTypeAssignment.from_dict(assignment)
                for assignment in publish_args.get("assignment", [])
            ]

            logger.info(f"Publishing assignments: {assignments}")

            # Process create/update operations first
            create_update_results = await self._process_create_update_operations(
                assignments, atlas_client, semaphore_key
            )

            # # Combine results
            results = {
                "create_update": create_update_results,
            }

            # Close Atlas client
            await atlas_client.close()

            return results

        except Exception as e:
            logger.error(f"Error executing publish: {str(e)}")
            raise e

    @activity.defn
    async def delete(self, delete_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Delete assets from Atlas based on the plan.
        """
        # workflow_id = get_workflow_id() or "atlas_publish"
        # semaphore_key = f"atlas_publish_{workflow_id}"

        try:
            # Initialize Atlas client
            atlas_client = AtlasClient(
                PublishConfig(username=delete_args.get("username", ""))
            )
            await atlas_client.initialize()

            assignments = [
                AssetTypeAssignment.from_dict(assignment)
                for assignment in delete_args.get("assignments", [])
            ]

            logger.info(f"Deleting assignments: {assignments}")

            # delete_results = await self._process_delete_operations(
            #     assignments, atlas_client, semaphore_key
            # )

            await atlas_client.close()

            # return delete_results

        except Exception as e:
            logger.error(f"Error executing delete: {str(e)}")
            raise e

    async def _process_create_update_operations(
        self,
        assignments: List[AssetTypeAssignment],
        client: AtlasClient,
        semaphore_key: str,
    ) -> Dict[str, Any]:
        """Process all create/update operations in order.

        Args:
            plan: Publish plan
            client: Atlas client
            config: Publish configuration
            state_store: DAPR state store client
            semaphore_key: Key for the semaphore

        Returns:
            Dictionary with results
        """
        results = {
            "layers": {},
            "successful_asset_types": 0,
            "failed_asset_types": 0,
            "total_records": 0,
            "successful_records": 0,
            "failed_records": 0,
        }

        for assignment in assignments:
            logger.info(f"Processing create/update for {assignment.asset_type}")
            layer_results = await self._process_layer(assignment, client, semaphore_key)
            results["layers"][assignment.asset_type] = layer_results

            return results

    # FIXME(inishchith): this is not implemented/tested yet
    async def _process_delete_operations(
        self,
        plan: PublishPlan,
        client: AtlasClient,
        config: PublishConfig,
        state_store: Any,
        semaphore_key: str,
    ) -> Dict[str, Any]:
        """Process all delete operations.

        Args:
            plan: Publish plan
            client: Atlas client
            config: Publish configuration
            state_store: DAPR state store client
            semaphore_key: Key for the semaphore

        Returns:
            Dictionary with results
        """
        results = {
            "successful_asset_types": 0,
            "failed_asset_types": 0,
            "total_records": 0,
            "successful_records": 0,
            "failed_records": 0,
            "asset_types": {},
        }

        # Get deletion assignments
        deletion_assignments = [
            assignment
            for assignment in plan.asset_type_assignments
            if assignment.is_deletion
        ]

        if not deletion_assignments:
            logger.info("No delete operations to process")
            return results

        # Process all deletions in parallel
        # We don't need to follow the order for deletions
        for assignment in deletion_assignments:
            asset_type = assignment.asset_type
            logger.info(f"Processing deletion for {asset_type}")

            # Create processor based on processing strategy
            processor = TypeLevelProcessor(client, config, state_store)

            # Process assignment
            try:
                # Acquire semaphore
                max_concurrency = config.max_activity_concurrency
                acquired = InMemorySemaphore.acquire(
                    semaphore_key, f"delete_{asset_type}", 1, max_concurrency
                )

                if not acquired:
                    logger.warning(
                        f"Could not acquire semaphore for {asset_type}, waiting..."
                    )
                    # FIXME(inishchith): Implement waiting logic here
                    # For now, just proceed anyway

                # Process assignment
                result = await processor.process(assignment)

                # Release semaphore
                InMemorySemaphore.release(semaphore_key, f"delete_{asset_type}", 1)

                # Update results
                results["asset_types"][asset_type] = result

                # Update success/failure counts
                if result.get("failed_records", 0) > 0:
                    if result.get("successful_records", 0) > 0:
                        # Partial success, count as success
                        results["successful_asset_types"] += 1
                    else:
                        results["failed_asset_types"] += 1
                else:
                    results["successful_asset_types"] += 1

                # Update record counts
                results["total_records"] += result.get("total_records", 0)
                results["successful_records"] += result.get("successful_records", 0)
                results["failed_records"] += result.get("failed_records", 0)

            except Exception as e:
                logger.error(f"Failed to process deletion for {asset_type}: {str(e)}")
                results["failed_asset_types"] += 1

                # Release semaphore
                InMemorySemaphore.release(semaphore_key, f"delete_{asset_type}", 1)

                # Record error
                results["asset_types"][asset_type] = {
                    "asset_type": asset_type,
                    "error": str(e),
                }

                # Check if we should continue
                if not config.continue_on_error:
                    logger.error("Stopping due to error and continue_on_error=False")
                    break

        return results

    async def _process_layer(
        self,
        assignments: AssetTypeAssignment,
        client: AtlasClient,
        # config: PublishConfig,
        # state_store: Any,
        semaphore_key: str,
        self_referential_types: List[str] = [],
    ) -> Dict[str, Any]:
        """Process a layer of asset type assignments.

        Args:
            assignments: List of assignments in the layer
            self_referential_types: List of self-referential types
            client: Atlas client
            config: Publish configuration
            state_store: DAPR state store client
            semaphore_key: Key for the semaphore

        Returns:
            Dictionary with results by asset type
        """
        results = {}

        # Process each assignment
        asset_type = assignments.asset_type
        logger.info(
            f"Processing {asset_type} with strategy {assignments.processing_strategy}"
        )

        # Create processor based on processing strategy and self-referential status
        if asset_type in self_referential_types:
            processor = SelfRefProcessor(client, client.config, None)
        elif assignments.processing_strategy == ProcessingStrategy.FILE_LEVEL:
            processor = FileLevelProcessor(client, client.config, None)
        else:
            processor = TypeLevelProcessor(client, client.config, None)

        # Process assignment
        try:
            # Acquire semaphore
            max_concurrency = client.config.max_activity_concurrency
            acquired = InMemorySemaphore.acquire(
                semaphore_key, f"process_{asset_type}", 1, max_concurrency
            )
            if not acquired:
                logger.warning(
                    f"Could not acquire semaphore for {asset_type}, waiting..."
                )
                # FIXME(inishchith): Implement waiting logic here
                # For now, just proceed anyway

            # Process assignment
            result = await processor.process(assignments)

            # Release semaphore
            InMemorySemaphore.release(semaphore_key, f"process_{asset_type}", 1)

            # Record result
            results[asset_type] = result

        except Exception as e:
            logger.error(f"Failed to process {asset_type}: {str(e)}")

            # Release semaphore
            InMemorySemaphore.release(semaphore_key, f"process_{asset_type}", 1)

            # Record error
            results[asset_type] = {
                "asset_type": asset_type,
                "error": str(e),
                "successful_records": 0,
                "failed_records": 0,
                "total_records": 0,
            }

            # Check if we should continue
            if not client.config.continue_on_error:
                logger.error("Stopping due to error and continue_on_error=False")
                raise e

        return results

    async def publish_classifications(self):
        """
        Publish classifications to Atlas.

        Note:
            This is currently a placeholder implementation.
        """
        pass

    async def publish_custom_attributes(self):
        """
        Publish custom attributes to Atlas.

        Note:
            This is currently a placeholder implementation.
        """
        pass
