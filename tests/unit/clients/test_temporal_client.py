from typing import Any, Dict, Generator
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest

from application_sdk.clients.temporal import TemporalWorkflowClient
from application_sdk.workflows import WorkflowInterface


# Mock workflow class for testing
class MockWorkflow(WorkflowInterface):
    pass


@pytest.fixture
def temporal_client() -> TemporalWorkflowClient:
    return TemporalWorkflowClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )


@pytest.fixture
def mock_dapr_output_client() -> Generator[Mock, None, None]:
    with patch("application_sdk.outputs.statestore.DaprClient") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.__exit__.return_value = None
        yield mock_instance


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_load(mock_connect: AsyncMock, temporal_client: TemporalWorkflowClient):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()

    # Verify that Client.connect was called with the correct parameters
    mock_connect.assert_called_once_with(
        temporal_client.get_connection_string(),
        namespace=temporal_client.get_namespace(),
    )

    # Check that client is set
    assert temporal_client.client == mock_client


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    temporal_client: TemporalWorkflowClient,
    mock_dapr_output_client: Mock,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    mock_handle = MagicMock()
    mock_handle.id = "test_workflow_id"
    mock_handle.result_run_id = "test_run_id"

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.return_value = mock_handle

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {"param1": "value1", "credentials": credentials}

    workflow_class = MockWorkflow

    # Run start_workflow and capture the result
    result = await temporal_client.start_workflow(workflow_args, workflow_class)

    # Assertions
    mock_client.start_workflow.assert_called_once()
    assert mock_dapr_output_client.save_state.call_count == 2  # Expect two calls
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow_with_workflow_id(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    temporal_client: TemporalWorkflowClient,
    mock_dapr_output_client: Mock,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    def start_workflow_side_effect(
        workflow_class: type[WorkflowInterface],
        args: Dict[str, Any],
        id: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> Mock:
        mock_handle = MagicMock()
        mock_handle.id = id
        mock_handle.result_run_id = "test_run_id"
        return mock_handle

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.side_effect = start_workflow_side_effect

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {
        "param1": "value1",
        "credentials": credentials,
        "workflow_id": "test_workflow_id",
    }

    workflow_class = MockWorkflow

    # Run start_workflow and capture the result
    result = await temporal_client.start_workflow(
        workflow_args,
        workflow_class,
    )

    # Assertions
    mock_client.start_workflow.assert_called_once()
    mock_dapr_output_client.save_state.assert_called_once()
    assert "workflow_id" in result
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow_failure(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    temporal_client: TemporalWorkflowClient,
    mock_dapr_output_client: Mock,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.side_effect = Exception("Simulated failure")

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {"param1": "value1", "credentials": credentials}

    workflow_class = MockWorkflow

    # Assertions
    with pytest.raises(Exception, match="Simulated failure"):
        await temporal_client.start_workflow(workflow_args, workflow_class)
    mock_client.start_workflow.assert_called_once()
    mock_dapr_output_client.save_state.assert_called()


@patch("application_sdk.clients.temporal.Worker")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_worker_without_client(
    mock_connect: AsyncMock,
    mock_worker_class: MagicMock,
    temporal_client: TemporalWorkflowClient,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Mock workflow class and activities
    workflow_classes = [MagicMock(), MagicMock()]
    activities = [MagicMock(), MagicMock()]
    passthrough_modules = ["application_sdk", "os"]

    # Run create_worker
    with pytest.raises(ValueError, match="Client is not loaded"):
        temporal_client.create_worker(activities, workflow_classes, passthrough_modules)


@patch("application_sdk.clients.temporal.Worker")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_worker(
    mock_connect: AsyncMock,
    mock_worker_class: MagicMock,
    temporal_client: TemporalWorkflowClient,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()

    # Mock workflow class and activities
    workflow_classes = [MagicMock(), MagicMock()]
    activities = [MagicMock(), MagicMock()]
    passthrough_modules = ["application_sdk", "os"]

    # Run create_worker
    worker = temporal_client.create_worker(
        activities, workflow_classes, passthrough_modules
    )

    # Verify Worker was instantiated with the expected parameters
    mock_worker_class.assert_called_once_with(
        temporal_client.client,
        task_queue=temporal_client.worker_task_queue,
        workflows=workflow_classes,
        activities=activities,
        workflow_runner=ANY,
        interceptors=ANY,
        activity_executor=ANY,
        max_concurrent_activities=ANY,
    )

    assert worker == mock_worker_class.return_value


def test_get_worker_task_queue(temporal_client: TemporalWorkflowClient):
    """Test get_worker_task_queue returns the application name."""
    assert temporal_client.get_worker_task_queue() == "test_app"


def test_get_connection_string(temporal_client: TemporalWorkflowClient):
    """Test get_connection_string returns properly formatted connection string."""
    assert temporal_client.get_connection_string() == "localhost:7233"


def test_get_namespace(temporal_client: TemporalWorkflowClient):
    """Test get_namespace returns the correct namespace."""
    assert temporal_client.get_namespace() == "default"


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_close(mock_connect: AsyncMock, temporal_client: TemporalWorkflowClient):
    """Test close method."""
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()

    # Close should complete without errors
    await temporal_client.close()


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_get_workflow_run_status_error(
    mock_connect: AsyncMock, temporal_client: TemporalWorkflowClient
):
    """Test get_workflow_run_status error handling."""
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Mock workflow handle with unexpected error
    mock_handle = AsyncMock()
    mock_handle.describe = AsyncMock(
        side_effect=Exception("Error getting workflow status")
    )
    mock_client.get_workflow_handle = AsyncMock(return_value=mock_handle)

    # Run load to connect the client
    await temporal_client.load()

    # Verify error is raised with correct message
    with pytest.raises(Exception, match="Error getting workflow status"):
        await temporal_client.get_workflow_run_status("test_workflow_id", "test_run_id")


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_get_workflow_run_status_client_not_loaded(
    mock_connect: AsyncMock, temporal_client: TemporalWorkflowClient
):
    """Test get_workflow_run_status when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.get_workflow_run_status("test_workflow_id", "test_run_id")
