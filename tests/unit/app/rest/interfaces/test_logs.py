from datetime import UTC, datetime
from typing import List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from application_sdk.app.models import Base, Log
from application_sdk.app.rest.interfaces.logs import Logs


# Setting up an in-memory SQLite database for testing
@pytest.fixture(scope="function")
def session():
    """Fixture for setting up a database session for testing."""
    # Create an in-memory SQLite database
    engine = create_engine("sqlite:///:memory:", echo=True)
    Base.metadata.create_all(engine)

    # Create a session to interact with the database
    Session = sessionmaker(bind=engine)
    session = Session()

    # Yield the session to be used in the tests
    yield session

    # Teardown: Close the session and drop all tables
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def setup_logs(session: Session):
    """Fixture to insert mock logs into the test database."""
    # Add sample log data
    logs = [
        Log(
            body="Error: something went wrong",
            timestamp=datetime(2023, 1, 1, tzinfo=UTC),
            observed_timestamp=datetime(2023, 1, 1, tzinfo=UTC),
            severity="ERROR",
            severity_number=17,
        ),
        Log(
            body="Warning: low disk space",
            timestamp=datetime(2023, 1, 5, tzinfo=UTC),
            observed_timestamp=datetime(2023, 1, 5, tzinfo=UTC),
            severity="WARN",
            severity_number=13,
        ),
        Log(
            body="Info: task completed",
            timestamp=datetime(2023, 1, 10, tzinfo=UTC),
            observed_timestamp=datetime(2023, 1, 10, tzinfo=UTC),
            severity="INFO",
            severity_number=9,
        ),
    ]

    session.add_all(logs)
    session.commit()

    return logs


def test_get_logs_within_timestamp_range(session: Session, setup_logs: List[Log]):
    """Test retrieving logs within a specific timestamp range."""
    from_timestamp = str(int(datetime(2023, 1, 1, tzinfo=UTC).timestamp()))
    to_timestamp = str(int(datetime(2023, 1, 7, tzinfo=UTC).timestamp()))

    logs = Logs.get_logs(
        session,
        query_dict={"timestamp__ge": from_timestamp, "timestamp__le": to_timestamp},
    )

    assert len(logs) == 2
    assert str(logs[0].body) == "Warning: low disk space"
    assert str(logs[1].body) == "Error: something went wrong"


def test_get_logs_with_keyword_filter(session: Session, setup_logs: List[Log]):
    """Test retrieving logs filtered by a keyword."""
    keyword = "Error"
    logs = Logs.get_logs(session, query_dict={"body__contains": keyword})

    assert len(logs) == 1
    assert str(logs[0].body) == "Error: something went wrong"


def test_get_logs_with_pagination(session: Session, setup_logs: List[Log]):
    """Test retrieving logs with pagination (skip and limit)."""
    logs = Logs.get_logs(session, skip=1, limit=1)

    assert len(logs) == 1
    assert str(logs[0].body) == "Warning: low disk space"
