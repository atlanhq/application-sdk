from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str
    event_type: str
    application_name: str
    attributes: Dict[str, Any]
    timestamp: Optional[datetime] = Field(default=None, init=False)


class GenericEvent(Event):
    event_type: str = Field(default="generic", init=False)

    # Optional: Activity information
    activity_name: Optional[str] = Field(default=None, init=False)
    activity_id: Optional[str] = Field(default=None, init=False)

    # Optional: Workflow information
    workflow_name: Optional[str] = Field(default=None, init=False)
    workflow_id: Optional[str] = Field(default=None, init=False)
    workflow_run_id: Optional[str] = Field(default=None, init=False)


class ApplicationEvent(GenericEvent):
    status: str


class ActivityStartEvent(GenericEvent):
    event_type: str = Field(default="activity_start", init=False)

    # Activity information (required)
    activity_name: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class ActivityEndEvent(GenericEvent):
    event_type: str = Field(default="activity_end", init=False)

    # Activity information (required)
    activity_name: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class WorkflowStartEvent(GenericEvent):
    event_type: str = Field(default="workflow_start", init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None, init=False)
    workflow_id: str | None = Field(default=None, init=False)
    workflow_run_id: str | None = Field(default=None, init=False)


class WorkflowEndEvent(GenericEvent):
    event_type: str = Field(default="workflow_end", init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None, init=False)
    workflow_id: str | None = Field(default=None, init=False)
    workflow_run_id: str | None = Field(default=None, init=False)
