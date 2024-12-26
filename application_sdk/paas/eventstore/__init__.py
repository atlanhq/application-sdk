"""Event store for the application."""

import json
import logging
from datetime import datetime

from dapr import clients
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.paas.eventstore.models import (
    ActivityEndEvent,
    ActivityStartEvent,
    CustomEvent,
    Event,
    WorkflowEndEvent,
    WorkflowStartEvent,
)

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


# TODO: Singleton, and client as instance attribute
class EventStore:
    EVENT_STORE_NAME = "eventstore"
    TOPIC_NAME = "app_events"
    APPLICATION_TOPIC_NAME = "application_events"

    # TODO:
    # client: DaprClient

    @classmethod
    def create_generic_event(cls, event: Event, topic_name: str = TOPIC_NAME):
        """
        Create a new generic event.

        :param event: Event data.
        :param topic_name: Topic name to publish the event to.

        Usage:
            >>> EventStore.create_generic_event(Event(event_type="test", data={"test": "test"}))
        """
        with clients.DaprClient() as client:
            client.publish_event(
                pubsub_name="eventstore",
                topic_name=topic_name,
                data=json.dumps(event.model_dump(mode="json")),
                data_content_type="application/json",
            )

        activity.logger.info(f"Published event to {topic_name}")

    @classmethod
    def create_workflow_start_event(cls, event: WorkflowStartEvent, topic_name: str):
        cls.create_generic_event(event, topic_name=topic_name)

    @classmethod
    def create_workflow_end_event(cls, event: WorkflowEndEvent, topic_name: str):
        cls.create_generic_event(event, topic_name=topic_name)

    @classmethod
    def create_activity_start_event(cls, event: ActivityStartEvent, topic_name: str):
        cls.create_generic_event(event, topic_name=topic_name)

    @classmethod
    def create_activity_end_event(cls, event: ActivityEndEvent, topic_name: str):
        cls.create_generic_event(event, topic_name=topic_name)

    @classmethod
    def create_custom_event(cls, event: CustomEvent, topic_name: str):
        cls.create_generic_event(event, topic_name=topic_name)
