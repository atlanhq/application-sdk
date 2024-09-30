"""Events for the application."""
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from application_sdk.app.models import Event
from application_sdk.app.rest.schemas import EventCreate


class Events:
    @staticmethod
    def get_event(session: Session, event_id: int) -> Optional[Event]:
        """
        Retrieve an event by ID.

        :param session: Database session.
        :param event_id: ID of the event to retrieve.
        :return: An Event object.
        """
        return session.query(Event).filter(Event.id == event_id).first()

    @staticmethod
    def get_events(
        session: Session, skip: int = 0, limit: int = 100
    ) -> Sequence[Event]:
        """
        Retrieve a list of events.

        :param session: Database session.
        :param skip: Number of events to skip (for pagination).
        :param limit: Maximum number of events to return.
        :return: A list of Event objects.
        """
        return session.query(Event).offset(skip).limit(limit).all()

    @staticmethod
    def create_event(session: Session, event: EventCreate) -> Event:
        """
        Create a new event.

        :param session: Database session.
        :param event: EventCreate object containing event data.
        :return: An Event object.
        """
        db_event = Event(
            name=event.name,
            event_type=event.event_type,
            status=event.status,
            application_name=event.application_name,
            attributes=event.attributes,
            observed_timestamp=event.observed_timestamp,
        )
        session.add(db_event)
        session.commit()
        session.refresh(db_event)
        return db_event
