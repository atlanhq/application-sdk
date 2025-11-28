from typing import Any, Dict, Generator
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest

from application_sdk.clients.temporal import TemporalWorkflowClient
from application_sdk.interceptors.cleanup import cleanup
from application_sdk.interceptors.events import publish_event
from application_sdk.workflows import WorkflowInterface


# Mock workflow class for testing
class MockWorkflow(WorkflowInterface):
    pass


@pytest.fixture
def temporal_client() -> TemporalWorkflowClient:
    """Create a TemporalWorkflowClient instance for testing."""

    def mock_get_deployment_secret(key: str):
        # Return None for all keys by default (tests can override if needed)
        return None

    with patch(
        "application_sdk.clients.temporal.SecretStore.get_deployment_secret",
        side_effect=mock_get_deployment_secret,
    ):
        return TemporalWorkflowClient(
            host="localhost",
            port="7233",
            application_name="test_app",
            namespace="default",
        )


@pytest.fixture
def mock_dapr_output_client() -> Generator[Mock, None, None]:
    """Mock Dapr output clients."""
    with patch(
        "application_sdk.clients.temporal.StateStore"
    ) as mock_state_output, patch(
        "application_sdk.services.statestore.StateStore.get_state"
    ) as mock_get_state, patch(
        "application_sdk.services.objectstore.ObjectStore.upload_file"
    ) as mock_push_file:
        mock_state_output.save_state = AsyncMock()
        mock_state_output.save_state_object = AsyncMock()
        mock_get_state.return_value = {}  # Return empty state
        mock_push_file.return_value = None  # Mock the push file operation
        yield mock_state_output


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
@patch("application_sdk.clients.temporal.SecretStore.get_deployment_secret")
async def test_load(
    mock_get_config: AsyncMock,
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test loading the temporal client."""
    # Mock the deployment config to return None for all keys (auth disabled)
    mock_get_config.return_value = None

    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()

    # Verify that Client.connect was called with the correct parameters
    mock_connect.assert_called_once_with(
        target_host=temporal_client.get_connection_string(),
        namespace=temporal_client.get_namespace(),
        tls=False,
    )

    # Check that client is set
    assert temporal_client.client == mock_client


@patch("application_sdk.services.secretstore.SecretStore")
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
    """Test starting a workflow."""
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
    assert (
        mock_dapr_output_client.save_state_object.call_count == 1
    )  # Expect one call when workflow_id not provided
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.services.secretstore.SecretStore")
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
    """Test starting a workflow with a provided workflow ID."""
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
    mock_dapr_output_client.save_state_object.assert_not_called()  # Should not be called when workflow_id is provided
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.services.secretstore.SecretStore")
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
    """Test workflow start failure handling."""
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
    mock_dapr_output_client.save_state_object.assert_called()


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
    """Test creating a worker without a loaded client."""
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
    """Test creating a worker with a loaded client."""
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

    expected_activities = list(activities) + [publish_event, cleanup]
    mock_worker_class.assert_called_once_with(
        temporal_client.client,
        task_queue=temporal_client.worker_task_queue,
        workflows=workflow_classes,
        activities=expected_activities,
        workflow_runner=ANY,
        interceptors=ANY,
        activity_executor=ANY,
        max_concurrent_activities=ANY,
    )

    assert worker == mock_worker_class.return_value


def test_get_worker_task_queue(temporal_client: TemporalWorkflowClient):
    """Test get_worker_task_queue returns the application name with deployment name."""
    assert temporal_client.get_worker_task_queue() == "atlan-test_app-local"


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


@patch("application_sdk.clients.temporal.logger")
@patch("application_sdk.clients.temporal.EventStore.publish_event")
@patch("application_sdk.clients.temporal.time.time")
@patch("application_sdk.clients.temporal.DEPLOYMENT_NAME", "test_deployment")
async def test_publish_token_refresh_event_success(
    mock_time: Mock,
    mock_publish_event: AsyncMock,
    mock_logger: MagicMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test successful token refresh event publishing."""
    # Arrange
    mock_time.return_value = 1234567890.0
    mock_auth_manager = MagicMock()
    mock_auth_manager.get_token_expiry_time.return_value = 1234567895.0
    mock_auth_manager.get_time_until_expiry.return_value = 300.0
    temporal_client.auth_manager = mock_auth_manager

    # Act
    await temporal_client._publish_token_refresh_event()

    # Assert
    mock_time.assert_called_once()
    mock_auth_manager.get_token_expiry_time.assert_called_once()
    mock_auth_manager.get_time_until_expiry.assert_called_once()
    mock_publish_event.assert_called_once()

    # Verify the event structure
    call_args = mock_publish_event.call_args
    event = call_args[0][0]  # First positional argument

    assert event.event_type == "application_events"
    assert event.event_name == "token_refresh"
    assert event.data["application_name"] == "test_app"
    assert event.data["deployment_name"] == "test_deployment"
    assert event.data["force_refresh"] is True
    assert event.data["token_expiry_time"] == 1234567895.0
    assert event.data["time_until_expiry"] == 300.0
    assert event.data["refresh_timestamp"] == 1234567890.0

    mock_logger.info.assert_called_once_with("Published token refresh event")
    mock_logger.warning.assert_not_called()


@patch("application_sdk.clients.temporal.logger")
@patch("application_sdk.clients.temporal.EventStore.publish_event")
@patch("application_sdk.clients.temporal.time.time")
@patch("application_sdk.clients.temporal.DEPLOYMENT_NAME", "test_deployment")
async def test_publish_token_refresh_event_with_none_expiry_times(
    mock_time: Mock,
    mock_publish_event: AsyncMock,
    mock_logger: MagicMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test token refresh event publishing with None expiry times."""
    # Arrange
    mock_time.return_value = 1234567890.0
    mock_auth_manager = MagicMock()
    mock_auth_manager.get_token_expiry_time.return_value = None
    mock_auth_manager.get_time_until_expiry.return_value = None
    temporal_client.auth_manager = mock_auth_manager

    # Act
    await temporal_client._publish_token_refresh_event()

    # Assert
    call_args = mock_publish_event.call_args
    event = call_args[0][0]  # First positional argument

    assert event.data["token_expiry_time"] == 0
    assert event.data["time_until_expiry"] == 0

    mock_logger.info.assert_called_once_with("Published token refresh event")
    mock_logger.warning.assert_not_called()


@patch("application_sdk.clients.temporal.logger")
@patch("application_sdk.clients.temporal.EventStore.publish_event")
@patch("application_sdk.clients.temporal.time.time")
async def test_publish_token_refresh_event_exception_handling(
    mock_time: Mock,
    mock_publish_event: AsyncMock,
    mock_logger: MagicMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test token refresh event publishing handles exceptions gracefully."""
    # Arrange
    mock_time.return_value = 1234567890.0
    mock_auth_manager = MagicMock()
    mock_auth_manager.get_token_expiry_time.return_value = 1234567895.0
    mock_auth_manager.get_time_until_expiry.return_value = 300.0
    temporal_client.auth_manager = mock_auth_manager

    # Simulate event publishing failure
    mock_publish_event.side_effect = Exception("Event store connection failed")

    # Act - should not raise exception
    await temporal_client._publish_token_refresh_event()

    # Assert
    mock_publish_event.assert_called_once()
    mock_logger.info.assert_not_called()
    mock_logger.warning.assert_called_once_with(
        "Failed to publish token refresh event: Event store connection failed"
    )
