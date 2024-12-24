from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class Event(BaseModel):
    event_type: str = Field(default="generic", init=False)


class ActivityStartEvent(Event):
    event_type: str = Field(default="activity_start", init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class ActivityEndEvent(Event):
    event_type: str = Field(default="activity_end", init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class WorkflowStartEvent(Event):
    activity_id: Optional[str] | None = Field(default=None)
    activity_name: Optional[str] | None = Field(default=None)
    application_name: str = Field()
    attributes: Dict[str, Any] = Field(default={})
    event_type: str = Field(default="workflow_start")
    name: Optional[str] = Field()
    published_timestamp: datetime = Field(default_factory=datetime.now)
    workflow_id: str = Field()
    workflow_name: str = Field()
    workflow_run_id: str = Field()
    id: str = Field(default_factory=lambda: uuid4().hex)


class WorkflowEndEvent(Event):
    event_type: str = Field(default="workflow_end")

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)


class CustomEvent(Event):
    pass


class DaprEvent(BaseModel):
    data: (
        WorkflowStartEvent
        | WorkflowEndEvent
        | ActivityEndEvent
        | ActivityStartEvent
        | CustomEvent
    )
    datacontenttype: str = Field()
    id: str = Field()
    pubsubname: str = Field()
    source: str = Field()
    specversion: str = Field()
    time: datetime = Field()
    topic: str = Field()
    traceid: str = Field()
    traceparent: str = Field()
    tracestate: str = Field()
    type: str = Field()
