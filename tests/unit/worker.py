from unittest.mock import AsyncMock, Mock

import pytest

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.worker import Worker


@pytest.fixture
def mock_workflow_client():
    workflow_client = Mock(spec=WorkflowClient)
    workflow_client.worker_task_queue = "test_queue"

    worker = Mock()
    worker.run = AsyncMock()
    worker.run.return_value = None

    workflow_client.create_worker = Mock()
    workflow_client.create_worker.return_value = worker
    return workflow_client


async def test_worker_should_raise_error_if_temporal_client_is_not_set():
    worker = Worker(workflow_client=None)
    with pytest.raises(ValueError, match="Workflow client is not set"):
        await worker.start()


async def test_worker_start_with_empty_activities_and_workflows(
    mock_workflow_client: WorkflowClient,
):
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[],
        workflow_classes=[],
        passthrough_modules=[],
    )
    await worker.start()

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start(mock_workflow_client: WorkflowClient):
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock(), AsyncMock()],
        passthrough_modules=["application_sdk", "os"],
    )
    await worker.start()

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore
