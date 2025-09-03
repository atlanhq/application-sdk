from typing import Any, Dict, Optional

from application_sdk.clients.base import BaseClient
from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class BaseHandler(HandlerInterface):
    """
    Base handler for non-SQL based applications.

    This class provides a base implementation for handlers that need to interact with non-SQL data sources. It implements the HandlerInterface and provides basic functionality that can be extended by subclasses.

    Attributes:
        client (BaseClient): The client instance for connecting to the target system.
    """

    def __init__(self, client: Optional[BaseClient] = None):
        """
        Initialize the base handler.

        Args:
            client (BaseClient, optional): The client instance to use for connections. Defaults to BaseClient().
        """
        self.client = client or BaseClient()

    async def load(self, credentials: Dict[str, Any]) -> None:
        """
        Load and initialize the handler.

        This method initializes the handler and loads the client with the provided credentials.

        Args:
            credentials (Dict[str, Any]): Credentials for the client.
        """
        logger.info("Loading base handler")

        # Load the client with credentials
        await self.client.load(credentials=credentials)

        logger.info("Base handler loaded successfully")

    # The following methods are inherited from HandlerInterface and should be implemented
    # by subclasses to handle calls from their respective FastAPI endpoints:
    # - test_auth(**kwargs) -> bool: Called by /workflow/v1/auth endpoint
    # - preflight_check(**kwargs) -> Any: Called by /workflow/v1/check endpoint
    # - fetch_metadata(**kwargs) -> Any: Called by /workflow/v1/metadata endpoint

    async def test_auth(self, **kwargs: Any) -> bool:
        """
        Test the authentication of the handler.
        """
        raise NotImplementedError("test_auth is not implemented")

    async def preflight_check(self, **kwargs: Any) -> Any:
        """
        Check the preflight of the handler.
        """
        raise NotImplementedError("preflight_check is not implemented")

    async def fetch_metadata(self, **kwargs: Any) -> Any:
        """
        Fetch the metadata of the handler.
        """
        raise NotImplementedError("fetch_metadata is not implemented")
