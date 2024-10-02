"""Event store for the application."""
import json
import logging

from application_sdk.paas.models import GenericEvent, ApplicationEvent
from dapr.clients import DaprClient

logger = logging.getLogger(__name__)


class EventStore:
    EVENT_STORE_NAME = "eventstore"
    TOPIC_NAME = "events"
    APPLICATION_TOPIC_NAME = "application_events"

    @classmethod
    def create_generic_event(cls, event: GenericEvent, topic_name: str = TOPIC_NAME):
        """
        Create a new generic event.

        :param event: Event data.
        :param topic_name: Topic name to publish the event to.

        Usage:
            >>> EventStore.create_generic_event(GenericEvent(event_type="test", data={"test": "test"}))
        """
        with DaprClient() as client:
            client.publish_event(
                pubsub_name=cls.EVENT_STORE_NAME,
                topic_name=topic_name,
                data=json.dumps(event.model_dump(mode='json')),
                data_content_type='application/json',
            )

    @classmethod
    def create_application_event(cls, event: ApplicationEvent):
        """
        Create a new application event.

        :param event: Event data.
        """
        cls.create_generic_event(event, topic_name=cls.APPLICATION_TOPIC_NAME)
