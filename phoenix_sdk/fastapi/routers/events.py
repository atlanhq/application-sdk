from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from phoenix_sdk.database import get_session
from phoenix_sdk.interfaces.events import Events
from phoenix_sdk.schemas import Event, EventCreate

router = APIRouter(
    prefix="/telemetry/v1/events",
    tags=["events"],
    responses={404: {"description": "Not found"}},
)


@router.get("", response_model=list[Event])
async def read_items(
    skip: int = 0, limit: int = 100, session: Session = Depends(get_session)
):
    return Events.get_events(session, skip, limit)


@router.get("/{event_id}", response_model=Event)
async def read_item(event_id: int, session: Session = Depends(get_session)):
    db_event: Optional[Event] = Events.get_event(session, event_id)
    if db_event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return db_event


@router.post("", response_model=Event)
async def create_item(event: EventCreate, session: Session = Depends(get_session)):
    return Events.create_event(session, event)
