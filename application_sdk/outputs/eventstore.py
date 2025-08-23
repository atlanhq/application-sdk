"""Event store module for handling application events.

This module provides the EventStore class for publishing application events
to a pub/sub system with automatic fallback to HTTP binding.
"""

import json
from datetime import datetime

from dapr import clients
from temporalio import activity, workflow

from application_sdk.clients.atlan_auth import AtlanAuthClient
from application_sdk.common.dapr_utils import is_component_registered
from application_sdk.constants import (
    APPLICATION_NAME,
    DAPR_BINDING_OPERATION_CREATE,
    EVENT_STORE_NAME,
)
from application_sdk.events.models import Event, EventMetadata, WorkflowStates
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class EventStore:
    """Event store for publishing application events.

    This class provides functionality to publish events to a pub/sub system.
    """

    @classmethod
    def enrich_event_metadata(cls, event: Event):
        """Enrich the event metadata with the workflow and activity information.

        Args:
            event (Event): Event data.
        """
        if not event.metadata:
            event.metadata = EventMetadata()

        event.metadata.application_name = APPLICATION_NAME
        event.metadata.created_timestamp = int(datetime.now().timestamp())
        event.metadata.topic_name = event.get_topic_name()

        try:
            workflow_info = workflow.info()
            if workflow_info:
                event.metadata.workflow_type = workflow_info.workflow_type
                event.metadata.workflow_id = workflow_info.workflow_id
                event.metadata.workflow_run_id = workflow_info.run_id
        except Exception:
            logger.debug("Not in workflow context, cannot set workflow metadata")

        try:
            activity_info = activity.info()
            if activity_info:
                event.metadata.activity_type = activity_info.activity_type
                event.metadata.activity_id = activity_info.activity_id
                event.metadata.attempt = activity_info.attempt
                event.metadata.workflow_type = activity_info.workflow_type
                event.metadata.workflow_id = activity_info.workflow_id
                event.metadata.workflow_run_id = activity_info.workflow_run_id
                event.metadata.workflow_state = WorkflowStates.RUNNING.value
        except Exception:
            logger.debug("Not in activity context, cannot set activity metadata")

        return event

    @classmethod
    async def publish_event(cls, event: Event):
        """Publish event with automatic fallback between pub/sub and HTTP binding.

        Args:
            event (Event): Event data to publish.
        """
        if not is_component_registered(EVENT_STORE_NAME):
            logger.warning(
                "Skipping event publish because event store component is not registered",
            )
            return
        try:
            event = cls.enrich_event_metadata(event)

            payload = json.dumps(event.model_dump(mode="json"))

            # Prepare binding metadata with auth token for HTTP bindings
            binding_metadata = {"content-type": "application/json"}

            # Add auth token - HTTP bindings will use it, others will ignore it
            auth_client = AtlanAuthClient()
            binding_metadata.update(await auth_client.get_authenticated_headers())

            with clients.DaprClient() as client:
                client.invoke_binding(
                    binding_name=EVENT_STORE_NAME,
                    operation=DAPR_BINDING_OPERATION_CREATE,
                    data=payload,
                    binding_metadata=binding_metadata,
                )
                logger.info(
                    f"Published event via binding on topic: {event.get_topic_name()}"
                )
        except Exception as e:
            logger.error(
                f"Failed to publish event on topic {event.get_topic_name()}: {e}",
                exc_info=True,
            )
