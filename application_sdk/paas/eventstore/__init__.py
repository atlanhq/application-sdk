"""Event store for the application."""

import json
import logging

from dapr import clients
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.paas.eventstore.models import Event

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class EventStore:
    EVENT_STORE_NAME = "eventstore"
    TOPIC_NAME = "app_events"

    @classmethod
    def create_event(cls, event: Event, topic_name: str = TOPIC_NAME):
        """
        Create a new generic event.

        :param event: Event data.
        :param topic_name: Topic name to publish the event to.

        Usage:
            >>> EventStore.create_generic_event(Event(event_type="test", data={"test": "test"}))
        """
        with clients.DaprClient() as client:
            client.publish_event(
                pubsub_name=cls.EVENT_STORE_NAME,
                topic_name=topic_name,
                data=json.dumps(event.model_dump(mode="json")),
                data_content_type="application/json",
            )

        activity.logger.info(f"Published event to {topic_name}")
