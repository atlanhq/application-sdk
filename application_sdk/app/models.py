"""Models for the database."""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.types import JSON

from application_sdk.app.database import Base


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_attributes = Column(JSON(), nullable=True)
    scope_name = Column(String, nullable=True)
    severity = Column(String, nullable=False)

    severity_number = Column(Integer, nullable=False)
    observed_timestamp = Column(DateTime, nullable=False, default=datetime.now)
    timestamp = Column(DateTime, nullable=False, default=datetime.now)
    body = Column(String, nullable=True)

    trace_id = Column(String, nullable=True)
    span_id = Column(String, nullable=True)
    attributes = Column(JSON(), nullable=True)


class Metric(Base):
    __tablename__ = "metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)

    scope_name = Column(String, nullable=True)
    observed_timestamp = Column(DateTime, nullable=False, default=datetime.now)
    timestamp = Column(DateTime, nullable=False, default=datetime.now)
    resource_attributes = Column(JSON(), nullable=True)
    unit = Column(String, nullable=True)
    data_points = Column(JSON(), nullable=False)


class Trace(Base):
    __tablename__ = "traces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String, nullable=False)
    span_id = Column(String, nullable=False)
    parent_span_id = Column(String, nullable=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)

    start_time = Column(DateTime, nullable=False, default=datetime.now)
    end_time = Column(DateTime, nullable=False, default=datetime.now)
    resource_attributes = Column(JSON(), nullable=True)
    attributes = Column(JSON(), nullable=True)
    events = Column(JSON(), nullable=True)

    timestamp = Column(DateTime, nullable=False, default=datetime.now)
