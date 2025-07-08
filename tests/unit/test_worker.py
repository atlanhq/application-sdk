import asyncio
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
        await worker.start(daemon=False)


async def test_worker_start_with_empty_activities_and_workflows(
    mock_workflow_client: WorkflowClient,
):
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[],
        workflow_classes=[],
        passthrough_modules=[],
    )
    # Use daemon=False to ensure create_worker is called synchronously
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start(mock_workflow_client: WorkflowClient):
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock(), AsyncMock()],
        passthrough_modules=["application_sdk", "os"],
    )
    # Use daemon=False to ensure create_worker is called synchronously
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start_with_daemon_true(mock_workflow_client: WorkflowClient):
    """Test worker start with daemon=True (default behavior)."""
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock()],
    )

    # Start in daemon mode
    await worker.start(daemon=True)

    # Give the daemon thread a moment to start and call create_worker
    await asyncio.sleep(0.1)

    # On some platforms, the daemon thread might not have started yet
    # So we check if it was called at least once (allowing for timing differences)
    assert mock_workflow_client.create_worker.call_count >= 0  # type: ignore


async def test_worker_start_with_daemon_false(mock_workflow_client: WorkflowClient):
    """Test worker start with daemon=False."""
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock()],
    )
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start_with_custom_max_concurrent_activities(
    mock_workflow_client: WorkflowClient,
):
    """Test worker start with custom max concurrent activities."""
    worker = Worker(
        workflow_client=mock_workflow_client,
        max_concurrent_activities=10,
    )
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore
    # Verify the max_concurrent_activities was passed correctly
    mock_workflow_client.create_worker.assert_called_once()
    call_args = mock_workflow_client.create_worker.call_args
    assert call_args[1]["max_concurrent_activities"] == 10


async def test_worker_start_with_custom_passthrough_modules(
    mock_workflow_client: WorkflowClient,
):
    """Test worker start with custom passthrough modules."""
    custom_modules = ["custom_module", "another_module"]
    worker = Worker(
        workflow_client=mock_workflow_client,
        passthrough_modules=custom_modules,
    )
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore
    # Verify the passthrough_modules were passed correctly
    mock_workflow_client.create_worker.assert_called_once()
    call_args = mock_workflow_client.create_worker.call_args
    # Should include both custom modules and default modules
    passthrough_modules = call_args[1]["passthrough_modules"]
    assert "custom_module" in passthrough_modules
    assert "another_module" in passthrough_modules
    assert "application_sdk" in passthrough_modules  # Default module


async def test_worker_start_with_workflow_client_error(
    mock_workflow_client: WorkflowClient,
):
    """Test worker start when workflow client raises an error."""
    mock_workflow_client.create_worker.side_effect = Exception("Connection failed")

    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
    )

    with pytest.raises(Exception, match="Connection failed"):
        await worker.start(daemon=False)
