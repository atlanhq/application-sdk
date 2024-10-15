import json
from datetime import UTC, datetime
from typing import List

import pytest
from google.protobuf import json_format
from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from application_sdk.app.models import Base, Trace
from application_sdk.app.rest.interfaces.traces import Traces


@pytest.fixture(scope="function")
def session():
    """Fixture for setting up a database session for testing."""
    # Create an in-memory SQLite database
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)  # Create tables based on your models

    # Create a session to interact with the database
    Session = sessionmaker(bind=engine)
    session = Session()

    # Yield the session to be used in the tests
    yield session

    # Teardown: Close the session and drop all tables
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def setup_traces(session: Session):
    """Fixture to insert mock traces into the test database."""
    # Add sample trace data
    traces = [
        Trace(
            id=1,
            name="trace_1",
            start_time=datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC),
            end_time=datetime(2023, 1, 1, 12, 5, 0, tzinfo=UTC),
            trace_id="1234abcd",
            span_id="abcd1234",
            parent_span_id="",
            kind=1,
            resource_attributes={"key1": "value1"},
            attributes={"attr1": "value1"},
            events=[],
        ),
        Trace(
            id=2,
            name="trace_2",
            start_time=datetime(2023, 1, 2, 12, 0, 0, tzinfo=UTC),
            end_time=datetime(2023, 1, 2, 12, 5, 0, tzinfo=UTC),
            trace_id="5678efgh",
            span_id="efgh5678",
            parent_span_id="",
            kind=1,
            resource_attributes={"key2": "value2"},
            attributes={"attr2": "value2"},
            events=[],
        ),
    ]

    session.add_all(traces)
    session.commit()

    return traces


def test_get_trace_by_id(session: Session, setup_traces: List[Trace]):
    trace = Traces.get_trace(session, 1)

    assert trace is not None
    assert str(trace.name) == "trace_1"
    assert str(trace.trace_id) == "1234abcd"


def test_get_traces_within_timestamp_range(session: Session, setup_traces: List[Trace]):
    """Test retrieving traces within a specific timestamp range."""
    from_timestamp = int(datetime(2023, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp())
    to_timestamp = int(datetime(2023, 1, 1, 23, 59, 59, tzinfo=UTC).timestamp())

    traces = Traces.get_traces(
        session, from_timestamp=from_timestamp, to_timestamp=to_timestamp
    )

    assert (
        len(traces) == 1
    )  # Only the trace with start time on Jan 1 should be returned
    assert str(traces[0].name) == "trace_1"
    assert str(traces[0].trace_id) == "1234abcd"


def test_create_traces(session: Session):
    """Test creating traces from a protobuf message."""
    mock_traces_data = {
        "resource_spans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"string_value": "example-service"},
                        }
                    ]
                },
                "scope_spans": [
                    {
                        "scope": {
                            "name": "example-instrumentation-scope",
                        },
                        "spans": [
                            {
                                "trace_id": "1234567890abcdef1234567890abcdef",
                                "span_id": "abcdef1234567890",
                                "parent_span_id": "1234567890abcdef",
                                "name": "example-span",
                                "kind": 1,  # Assuming 1 = SPAN_KIND_INTERNAL, based on OpenTelemetry spec
                                "start_time_unix_nano": int(
                                    datetime(2023, 1, 1, 0, 0, tzinfo=UTC).timestamp()
                                    * 1e9
                                ),
                                "end_time_unix_nano": int(
                                    datetime(2023, 1, 1, 0, 5, tzinfo=UTC).timestamp()
                                    * 1e9
                                ),
                                "attributes": [
                                    {
                                        "key": "http.method",
                                        "value": {"string_value": "GET"},
                                    },
                                    {
                                        "key": "http.url",
                                        "value": {
                                            "string_value": "https://example.com"
                                        },
                                    },
                                ],
                                "events": [
                                    {
                                        "time_unix_nano": int(
                                            datetime(
                                                2023, 1, 1, 0, 1, tzinfo=UTC
                                            ).timestamp()
                                            * 1e9
                                        ),
                                        "name": "request_received",
                                        "attributes": [
                                            {
                                                "key": "event.attribute.key",
                                                "value": {
                                                    "string_value": "event.attribute.value"
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "time_unix_nano": int(
                                            datetime(
                                                2023, 1, 1, 0, 2, tzinfo=UTC
                                            ).timestamp()
                                            * 1e9
                                        ),
                                        "name": "response_sent",
                                        "attributes": [
                                            {
                                                "key": "response.attribute.key",
                                                "value": {
                                                    "string_value": "response.attribute.value"
                                                },
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    traces_message = TracesData()
    json_format.Parse(json.dumps(mock_traces_data), traces_message)

    # Call the method
    created_traces = Traces.create_traces(session, traces_message)

    # Assert the correct trace was created
    assert len(created_traces) == 1
    assert str(created_traces[0].name) == "example-span"
    assert (
        str(created_traces[0].trace_id)
        == "d76df8e7aefcf7469b71d79fd76df8e7aefcf7469b71d79f"
    )
    assert str(created_traces[0].events[0]["name"]) == "request_received"


def test_get_traces_with_pagination(session: Session, setup_traces: List[Trace]):
    """Test retrieving traces with pagination (skip and limit)."""
    traces = Traces.get_traces(session, skip=1, limit=1)

    assert len(traces) == 1
    assert str(traces[0].name) == "trace_1"
    assert str(traces[0].trace_id) == "1234abcd"
