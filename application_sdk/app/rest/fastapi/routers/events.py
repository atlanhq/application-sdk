"""Router for handling event-related API endpoints."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from application_sdk.app.database import get_session
from application_sdk.paas.events import Events
from application_sdk.app.rest.schemas import Event, EventCreate

router = APIRouter(
    prefix="/telemetry/v1/events",
    tags=["events"],
    responses={404: {"description": "Not found"}},
)


@router.get("", response_model=list[Event])
async def read_events(
    skip: int = 0, limit: int = 100, session: Session = Depends(get_session)
):
    """Retrieve a list of events.

    :param skip: Number of events to skip (for pagination).
    :param limit: Maximum number of events to return.
    :param session: Database session.
    :return: A list of Event objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        return Events.get_events(session, skip, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{event_id}", response_model=Event)
async def read_event(event_id: int, session: Session = Depends(get_session)):
    """Retrieve a single event by ID.

    :param event_id: ID of the event to retrieve.
    :param session: Database session.
    :return: An Event object.
    :raises HTTPException: If the event is not found.
    """
    try:
        db_event: Optional[Event] = Events.get_event(session, event_id)
        if db_event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return db_event
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=Event)
async def create_event(event: EventCreate, session: Session = Depends(get_session)):
    """Create a new event.

    :param event: Event object to create.
    :param session: Database session.
    :return: The created Event object.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        return Events.create_event(session, event)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
