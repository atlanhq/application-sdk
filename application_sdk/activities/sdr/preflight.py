"""Preflight check SDR activity."""

from typing import Any, Dict, Optional, Type

from temporalio import activity

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.activities.sdr.utils import create_handler
from application_sdk.clients import ClientInterface
from application_sdk.handlers import HandlerInterface


class PreflightCheckActivities(ActivitiesInterface):
    """Activity class for performing connector preflight checks."""

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
    async def preflight(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Perform preflight checks for the connector.

        Args:
            workflow_args: Must contain either 'credential_guid' or 'credentials',
                plus any connector-specific metadata.

        Returns:
            A dict of preflight check results.
        """
        handler = await create_handler(
            self.client_class, self.handler_class, workflow_args
        )
        return await handler.preflight_check(workflow_args)
