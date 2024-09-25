from typing import Optional, Sequence

from sqlalchemy.orm import Session

from phoenix_sdk.models import Event
from phoenix_sdk.schemas import EventCreate


class Events:
    @staticmethod
    def get_event(session: Session, event_id: int) -> Optional[Event]:
        return session.query(Event).filter(Event.id == event_id).first()

    @staticmethod
    def get_events(
        session: Session, skip: int = 0, limit: int = 100
    ) -> Sequence[Event]:
        return session.query(Event).offset(skip).limit(limit).all()

    @staticmethod
    def create_event(session: Session, event: EventCreate) -> Event:
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
