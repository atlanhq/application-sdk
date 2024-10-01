from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel


class GenericEvent(BaseModel):
    id: str = uuid4().hex
    name: str
    event_type: str
    application_name: str
    attributes: dict
    observed_timestamp: datetime
    timestamp: datetime


class ApplicationEvent(GenericEvent):
    status: str