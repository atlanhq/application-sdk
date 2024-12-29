from datetime import datetime
from typing import Any, Dict

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


class WorkflowEndEvent(Event):
    event_type: str = Field(default="workflow_end")

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)

    workflow_output: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStartEvent(Event):
    event_type: str = Field(default="workflow_start")

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)


class CustomEvent(Event):
    event_type: str = Field(default="custom")
    data: Dict[str, Any] = Field(default_factory=dict)


# TODO: Rename
class DaprEvent(BaseModel):
    data: WorkflowEndEvent | ActivityEndEvent | ActivityStartEvent | CustomEvent
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
