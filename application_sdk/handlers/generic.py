from typing import Any, Dict

from application_sdk.clients.generic import GenericClient
from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class GenericHandler(HandlerInterface):
    """
    Generic handler for non-SQL based applications.

    This class provides a base implementation for handlers that need to interact with non-SQL data sources. It implements the HandlerInterface and provides basic functionality that can be extended by subclasses.

    Attributes:
        client (GenericClient): The client instance for connecting to the target system.
    """

    def __init__(self, client: GenericClient | None = None):
        """
        Initialize the generic handler.

        Args:
            client (GenericClient, optional): The client instance to use for connections. Defaults to GenericClient().
        """
        self.client = client or GenericClient()

    async def load(self, credentials: Dict[str, Any]) -> None:
        """
        Load and initialize the handler.

        This method initializes the handler and loads the client with the provided credentials.

        Args:
            credentials (Dict[str, Any]): Credentials for the client.
        """
        logger.info("Loading generic handler")

        # Load the client with credentials
        await self.client.load(credentials=credentials)

        logger.info("Generic handler loaded successfully")

    # The following methods are inherited from HandlerInterface and should be implemented
    # by subclasses to handle calls from their respective FastAPI endpoints:
    #
    # - test_auth(**kwargs) -> bool: Called by /workflow/v1/auth endpoint
    # - preflight_check(**kwargs) -> Any: Called by /workflow/v1/check endpoint
    # - fetch_metadata(**kwargs) -> Any: Called by /workflow/v1/metadata endpoint
