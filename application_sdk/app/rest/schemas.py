from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class EventBase(BaseModel):
    name: str
    event_type: str
    status: str
    application_name: str
    attributes: Dict[str, Any]
    observed_timestamp: datetime


class EventCreate(EventBase):
    pass


class Event(EventBase):
    id: int
    timestamp: datetime

    class Config:
        from_attributes = True


class LogBase(BaseModel):
    resource_attributes: Dict[str, Any]
    scope_name: str
    severity: str
    severity_number: int
    observed_timestamp: datetime
    timestamp: datetime
    body: Optional[str]
    trace_id: str
    span_id: str


class Log(LogBase):
    id: int

    class Config:
        from_attributes = True


class MetricBase(BaseModel):
    name: str
    description: str
    scope_name: str
    observed_timestamp: datetime
    timestamp: datetime
    resource_attributes: Dict[str, Any]
    unit: str
    data_points: Dict[str, Any]


class Metric(MetricBase):
    id: int

    class Config:
        from_attributes = True


class TraceBase(BaseModel):
    trace_id: str
    span_id: str
    start_time: datetime
    end_time: datetime
    parent_span_id: str
    name: str
    kind: str
    resource_attributes: Dict[str, Any]


class Trace(TraceBase):
    id: int
    timestamp: datetime

    class Config:
        from_attributes = True
