"""Fetch metadata SDR activity."""

from typing import Any, Dict, List, Optional, Type

from temporalio import activity

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.activities.sdr.utils import create_handler
from application_sdk.clients import ClientInterface
from application_sdk.handlers import HandlerInterface


class FetchMetadataActivities(ActivitiesInterface):
    """Activity class for fetching connector filter metadata."""

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
    async def fetch_metadata(
        self, workflow_args: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Fetch filter metadata from the connector.

        Args:
            workflow_args: Must contain either 'credential_guid' or 'credentials'.
                May also contain 'metadata_type' and 'database' for SQL handlers.

        Returns:
            A list of metadata dictionaries for UI filter population.
        """
        client, handler = await create_handler(
            self.client_class, self.handler_class, workflow_args
        )
        try:
            # Pass only handler-relevant keys, not the full workflow_args.
            # workflow_args contains infrastructure keys (credential_guid,
            # workflow_id, output_path, etc.) that would cause a TypeError
            # in handlers with strict signatures like BaseSQLHandler
            # .fetch_metadata(metadata_type, database).
            #
            # This mirrors the FastAPI server (server/fastapi/__init__.py)
            # which extracts metadata_type and database from the request
            # body before calling the handler.  All handler implementations
            # accept **kwargs via HandlerInterface, so passing these named
            # params is safe — handlers that don't need them simply ignore
            # the extra kwargs.
            metadata_type = workflow_args.get("metadata_type")
            database = workflow_args.get("database", "")
            return await handler.fetch_metadata(
                metadata_type=metadata_type, database=database
            )
        finally:
            await client.close()
