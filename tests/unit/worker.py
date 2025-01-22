from unittest.mock import AsyncMock, Mock

import pytest

from application_sdk.clients.temporal import TemporalClient
from application_sdk.worker import Worker


@pytest.fixture
def mock_temporal_client():
    temporal_client = Mock(spec=TemporalClient)
    temporal_client.worker_task_queue = "test_queue"

    worker = Mock()
    worker.run = AsyncMock()
    worker.run.return_value = None

    temporal_client.create_worker = Mock()
    temporal_client.create_worker.return_value = worker
    return temporal_client


async def test_worker_should_raise_error_if_temporal_client_is_not_set():
    worker = Worker(temporal_client=None)
    with pytest.raises(ValueError, match="Temporal client is not set"):
        await worker.start()


async def test_worker_start_with_empty_activities_and_workflows(
    mock_temporal_client: TemporalClient,
):
    worker = Worker(
        temporal_client=mock_temporal_client,
        temporal_activities=[],
        workflow_classes=[],
        passthrough_modules=[],
    )
    await worker.start()

    assert mock_temporal_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start(mock_temporal_client: TemporalClient):
    worker = Worker(
        temporal_client=mock_temporal_client,
        temporal_activities=[AsyncMock()],
        workflow_classes=[AsyncMock(), AsyncMock()],
        passthrough_modules=["application_sdk", "os"],
    )
    await worker.start()

    assert mock_temporal_client.create_worker.call_count == 1  # type: ignore
