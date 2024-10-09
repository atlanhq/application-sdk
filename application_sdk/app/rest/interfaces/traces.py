"""Interface for handling trace-related API endpoints."""

import time
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence

from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
from sqlalchemy.orm import Session

from application_sdk.app.models import Trace


class Traces:
    @staticmethod
    def get_trace(session: Session, trace_id: int) -> Trace:
        return session.query(Trace).filter(Trace.id == trace_id).first()

    @staticmethod
    def get_traces(
        session: Session,
        skip: int = 0,
        limit: int = 100,
        from_timestamp: int = 0,
        to_timestamp: Optional[int] = None,
    ) -> Sequence[Trace]:
        if to_timestamp is None:
            to_timestamp = int(time.time())
        return (
            session.query(Trace)
            .filter(
                Trace.start_time >= datetime.fromtimestamp(from_timestamp, tz=UTC),
                Trace.end_time <= datetime.fromtimestamp(to_timestamp, tz=UTC),
            )
            .order_by(Trace.start_time.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def create_traces(session: Session, traces_data: TracesData) -> list[Trace]:
        traces: list[Trace] = []
        for resource_span in traces_data.resource_spans:
            resource_attributes = {}
            for resource_attribute in resource_span.resource.attributes:
                resource_attributes[resource_attribute.key] = (
                    resource_attribute.value.string_value
                )

            for scope_span in resource_span.scope_spans:
                for span in scope_span.spans:
                    attributes = {}
                    for attribute in span.attributes:
                        attributes[attribute.key] = attribute.value.string_value

                    events: List[Dict[str, Any]] = []
                    for event in span.events:
                        event_data: Dict[str, Any] = {
                            "name": event.name,
                            "timestamp_unix_nano": event.time_unix_nano,
                        }
                        event_attributes: Dict[str, Any] = {}
                        for attribute in event.attributes:
                            event_attributes[attribute.key] = (
                                attribute.value.string_value
                            )
                        event_data["attributes"] = event_attributes
                        events.append(event_data)

                    db_trace = Trace(
                        resource_attributes=resource_attributes,
                        name=span.name,
                        start_time=datetime.fromtimestamp(
                            span.start_time_unix_nano // 1000000000, tz=UTC
                        ),
                        end_time=datetime.fromtimestamp(
                            span.end_time_unix_nano // 1000000000, tz=UTC
                        ),
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_span_id=span.parent_span_id.hex(),
                        kind=span.kind.real,
                        attributes=attributes,
                        events=events,
                    )
                    session.add(db_trace)
                    traces.append(db_trace)
        session.commit()

        for trace in traces:
            session.refresh(trace)
        return traces
