"""TestAuth SDR activity."""

from typing import Any, Dict, Optional, Type

from temporalio import activity

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.activities.sdr.utils import create_handler
from application_sdk.clients import ClientInterface
from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class TestAuthActivities(ActivitiesInterface):
    """Activity class for testing connector authentication credentials."""

    client_class: Type[ClientInterface]
    handler_class: Type[HandlerInterface]

    def __init__(
        self,
        client_class: Optional[Type[ClientInterface]] = None,
        handler_class: Optional[Type[HandlerInterface]] = None,
    ):
        if client_class is not None:
            self.client_class = client_class
        if handler_class is not None:
            self.handler_class = handler_class
        super().__init__()

    @activity.defn
    @auto_heartbeater
    async def test_auth(self, workflow_args: Dict[str, Any]) -> bool:
        """Test authentication credentials for the connector.

        Args:
            workflow_args: Must contain either 'credential_guid' or 'credentials'.

        Returns:
            True if authentication succeeds.
        """
        logger.info("Starting test_auth activity")
        handler = await create_handler(
            self.client_class, self.handler_class, workflow_args
        )
        result = await handler.test_auth()
        logger.info("test_auth completed: %s", result)
        return result
