from datetime import datetime
from typing import Any, Dict

from pydantic import BaseModel, Field

WORKFLOW_END_EVENT = "workflow_end"
WORKFLOW_START_EVENT = "workflow_start"
ACTIVITY_START_EVENT = "activity_start"
ACTIVITY_END_EVENT = "activity_end"
CUSTOM_EVENT = "custom"


class Event(BaseModel):
    event_type: str = Field(init=False)


class ActivityStartEvent(Event):
    event_type: str = Field(default=ACTIVITY_START_EVENT, init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class ActivityEndEvent(Event):
    event_type: str = Field(default=ACTIVITY_END_EVENT, init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class WorkflowEndEvent(Event):
    event_type: str = Field(default=WORKFLOW_END_EVENT, init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)

    workflow_output: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStartEvent(Event):
    event_type: str = Field(default=WORKFLOW_START_EVENT, init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)


class CustomEvent(Event):
    event_type: str = Field(default=CUSTOM_EVENT, init=False)
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
