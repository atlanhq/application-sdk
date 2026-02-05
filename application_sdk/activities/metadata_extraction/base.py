from typing import Any, Dict, Optional, Type

from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
    get_workflow_id,
)
from application_sdk.clients.base import BaseClient
from application_sdk.common.error_codes import ActivityError
from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME
from application_sdk.handlers.base import BaseHandler
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.atlan_storage import AtlanStorage
from application_sdk.services.secretstore import SecretStore
from application_sdk.transformers import TransformerInterface

logger = get_logger(__name__)
activity.logger = logger


class BaseMetadataExtractionActivitiesState(ActivitiesState):
    """State for base metadata extraction activities."""

    client: Optional[BaseClient] = None
    handler: Optional[BaseHandler] = None
    transformer: Optional[TransformerInterface] = None


class BaseMetadataExtractionActivities(ActivitiesInterface):
    """Base activities for non-SQL metadata extraction workflows."""

    _state: Dict[str, BaseMetadataExtractionActivitiesState] = {}

    client_class: Type[BaseClient] = BaseClient
    handler_class: Type[BaseHandler] = BaseHandler
    transformer_class: Optional[Type[TransformerInterface]] = None

    def __init__(
        self,
        client_class: Optional[Type[BaseClient]] = None,
        handler_class: Optional[Type[BaseHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
    ):
        """Initialize the base metadata extraction activities.

        Args:
            client_class: Client class to use. Defaults to BaseClient.
            handler_class: Handler class to use. Defaults to BaseHandler.
            transformer_class: Transformer class to use. Users must provide their own transformer implementation.
        """
        if client_class:
            self.client_class = client_class
        if handler_class:
            self.handler_class = handler_class
        if transformer_class:
            self.transformer_class = transformer_class

        super().__init__()

    async def _set_state(self, workflow_args: Dict[str, Any]):
        """Set up the state for the current workflow.

        Args:
            workflow_args: Arguments for the workflow.
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = BaseMetadataExtractionActivitiesState()

        await super()._set_state(workflow_args)

        state = self._state[workflow_id]

        # Initialize client
        client = self.client_class()
        # Extract credentials from state store if credential_guid is available
        if "credential_guid" in workflow_args:
            logger.info(
                f"Retrieving credentials for credential_guid: {workflow_args['credential_guid']}"
            )
            try:
                credentials = await SecretStore.get_credentials(
                    workflow_args["credential_guid"]
                )
                logger.info(
                    f"Successfully retrieved credentials with keys: {list(credentials.keys())}"
                )
                # Load the client with credentials
                await client.load(credentials=credentials)
            except Exception as e:
                logger.error(f"Failed to retrieve credentials: {e}")
                raise

        state.client = client

        # Initialize handler
        handler = self.handler_class(client=client)
        state.handler = handler

        # Initialize transformer if provided
        if self.transformer_class:
            transformer_params = {
                "connector_name": APPLICATION_NAME,
                "connector_type": APPLICATION_NAME,
                "tenant_id": APP_TENANT_ID,
            }
            state.transformer = self.transformer_class(**transformer_params)

    @activity.defn
    @auto_heartbeater
    async def upload_to_atlan(
        self, workflow_args: Dict[str, Any]
    ) -> ActivityStatistics:
        """Upload transformed data to Atlan storage.

        This activity uploads the transformed data from object store to Atlan storage
        (S3 via Dapr). It only runs if ENABLE_ATLAN_UPLOAD is set to true and the
        Atlan storage component is available.

        Args:
            workflow_args (Dict[str, Any]): Workflow configuration containing paths and metadata.

        Returns:
            ActivityStatistics: Upload statistics or skip statistics if upload is disabled.

        Raises:
            ValueError: If workflow_id or workflow_run_id are missing.
            ActivityError: If the upload fails with any migration errors when ENABLE_ATLAN_UPLOAD is true.
        """
        # Upload data from object store to Atlan storage
        # Use workflow_id/workflow_run_id as the prefix to migrate specific data
        migration_prefix = get_object_store_prefix(workflow_args["output_path"])
        logger.info(
            f"Starting migration from object store with prefix: {migration_prefix}"
        )
        upload_stats = await AtlanStorage.migrate_from_objectstore_to_atlan(
            prefix=migration_prefix
        )

        # Log upload statistics
        logger.info(
            f"Atlan upload completed: {upload_stats.migrated_files} files uploaded, "
            f"{upload_stats.failed_migrations} failed"
        )

        if upload_stats.failures:
            logger.error(f"Upload failed with {len(upload_stats.failures)} errors")
            for failure in upload_stats.failures:
                logger.error(f"Upload error: {failure}")

            # Mark activity as failed when there are upload failures
            raise ActivityError(
                f"{ActivityError.ATLAN_UPLOAD_ERROR}: Atlan upload failed with {len(upload_stats.failures)} errors. "
                f"Failed migrations: {upload_stats.failed_migrations}, "
                f"Total files: {upload_stats.total_files}"
            )

        return ActivityStatistics(
            total_record_count=upload_stats.migrated_files,
            chunk_count=upload_stats.total_files,
            typename="atlan-upload-completed",
        )
