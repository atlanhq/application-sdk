from typing import Any, Dict, Optional, Type

from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.utils import get_workflow_id
from application_sdk.clients.generic import GenericClient
from application_sdk.common.credential_utils import get_credentials
from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME
from application_sdk.handlers.generic import GenericHandler
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers import TransformerInterface

logger = get_logger(__name__)
activity.logger = logger


class GenericMetadataExtractionActivitiesState(ActivitiesState):
    """State for generic metadata extraction activities."""

    def __init__(self):
        """Initialize the state."""
        self.client: Optional[GenericClient] = None
        self.handler: Optional[GenericHandler] = None
        self.transformer: Optional[TransformerInterface] = None


class GenericMetadataExtractionActivities(ActivitiesInterface):
    """Generic activities for non-SQL metadata extraction workflows."""

    _state: Dict[str, GenericMetadataExtractionActivitiesState] = {}

    client_class: Type[GenericClient] = GenericClient
    handler_class: Type[GenericHandler] = GenericHandler
    transformer_class: Optional[Type[TransformerInterface]] = None

    def __init__(
        self,
        client_class: Optional[Type[GenericClient]] = None,
        handler_class: Optional[Type[GenericHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
    ):
        """Initialize the generic metadata extraction activities.

        Args:
            client_class: Client class to use. Defaults to GenericClient.
            handler_class: Handler class to use. Defaults to GenericHandler.
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
            self._state[workflow_id] = GenericMetadataExtractionActivitiesState()

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
                credentials = await get_credentials(workflow_args["credential_guid"])
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
