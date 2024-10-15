import pytest
import json
from typing import List
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, UTC
from application_sdk.app.models import Metric, Base
from opentelemetry.proto.metrics.v1.metrics_pb2 import MetricsData
from application_sdk.app.rest.interfaces.metrics import Metrics
from google.protobuf import json_format

@pytest.fixture(scope='function')
def session():
    """Fixture for setting up a database session for testing."""
    # Create an in-memory SQLite database
    engine = create_engine("sqlite:///:memory:", echo=True)
    Base.metadata.create_all(engine)  # Create tables based on your models

    # Create a session to interact with the database
    Session = sessionmaker(bind=engine)
    session = Session()

    # Yield the session to be used in the tests
    yield session

    # Teardown: Close the session and drop all tables
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(scope='function')
def setup_metrics(session: Session):
    """Fixture to insert mock metrics into the test database."""
    # Add sample metric data
    metrics = [
        Metric(
            id=1,
            name="cpu_usage",
            description="CPU usage over time",
            unit="percentage",
            data_points={"gauge": {"value": 50}},
            observed_timestamp=datetime(2023, 1, 1, tzinfo=UTC),
        ),
        Metric(
            id=2,
            name="memory_usage",
            description="Memory usage over time",
            unit="MB",
            data_points={"gauge": {"value": 2048}},
            observed_timestamp=datetime(2023, 1, 5, tzinfo=UTC),
        ),
        Metric(
            id=3,
            name="cpu_usage",
            description="CPU usage over time",
            unit="percentage",
            data_points={"gauge": {"value": 70}},
            observed_timestamp=datetime(2023, 1, 10, tzinfo=UTC),
        ),
    ]
    
    session.add_all(metrics)
    session.commit()

    return metrics


def test_get_metrics_within_timestamp_range(session: Session, setup_metrics: List[Metric]):
    """Test retrieving metrics within a specific timestamp range."""
    from_timestamp = int(datetime(2023, 1, 1, tzinfo=UTC).timestamp())
    to_timestamp = int(datetime(2023, 1, 7, tzinfo=UTC).timestamp())

    metrics_response = Metrics.get_metrics(session, from_timestamp=from_timestamp, to_timestamp=to_timestamp)
    
    assert len(metrics_response) == 2
    assert "cpu_usage" in metrics_response
    assert "memory_usage" in metrics_response


def test_create_metrics(session: Session):
    mock_metrics_data = {
        "resource_metrics": [{
            "resource": {
                "attributes": [

                ]
            },
            "scope_metrics": [{
                "metrics": [{
                    "name": "cpu_usage",
                    "description": "CPU usage over time",
                    "unit": "percentage",
                    "gauge": {
                        "data_points": [{
                            "as_int": 50,
                            "time_unix_nano": int(datetime(2023, 1, 1, tzinfo=UTC).timestamp() * 1e9)
                        }]
                    }
                }],
                "scope": {
                    "name": "cpu_usage",
                }
            }]
        }]
    }

    metric_message = MetricsData()
    json_format.Parse(json.dumps(mock_metrics_data), metric_message)

    # Call the method
    created_metrics = Metrics.create_metrics(session, metric_message)

    # Assert the correct metric was created
    assert len(created_metrics) == 1
    assert str(created_metrics[0].name) == "cpu_usage"
    

def test_get_metric_by_id(session: Session, setup_metrics: List[Metric]):
    """Test retrieving a specific metric by ID."""
    metric = Metrics.get_metric(session, 1)

    assert metric is not None
    assert str(metric.name) == "cpu_usage"
