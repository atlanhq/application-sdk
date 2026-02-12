from typing import Any, Dict, Generator
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest
from temporalio.client import ScheduleOverlapPolicy

from application_sdk.clients.temporal import TemporalWorkflowClient
from application_sdk.constants import PAUSE_SIGNAL, RESUME_SIGNAL
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
        graceful_shutdown_timeout=ANY,
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
async def test_pause_workflow(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test pausing a workflow sends the pause signal."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = MagicMock()
    mock_handle.signal = AsyncMock()
    mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

    await temporal_client.pause_workflow("test_workflow_id", "test_run_id")

    mock_client.get_workflow_handle.assert_called_once_with(
        "test_workflow_id", run_id="test_run_id"
    )
    mock_handle.signal.assert_called_once_with(PAUSE_SIGNAL)


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_pause_workflow_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test pause_workflow raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.pause_workflow("test_workflow_id", "test_run_id")


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_resume_workflow(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test resuming a workflow sends the resume signal."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = MagicMock()
    mock_handle.signal = AsyncMock()
    mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

    await temporal_client.resume_workflow("test_workflow_id", "test_run_id")

    mock_client.get_workflow_handle.assert_called_once_with(
        "test_workflow_id", run_id="test_run_id"
    )
    mock_handle.signal.assert_called_once_with(RESUME_SIGNAL)


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_resume_workflow_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test resume_workflow raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.resume_workflow("test_workflow_id", "test_run_id")


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


# --- Schedule Tests ---


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_schedule(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
    mock_dapr_output_client: Mock,
):
    """Test creating a schedule calls Temporal's create_schedule with correct args."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    schedule_args = {
        "cron_expression": "0 9 * * MON-FRI",
        "workflow_args": {"param1": "value1"},
        "note": "Test schedule",
    }

    result = await temporal_client.create_schedule(
        "test-schedule", schedule_args, MockWorkflow
    )

    mock_client.create_schedule.assert_called_once()
    call_args = mock_client.create_schedule.call_args
    assert call_args[0][0] == "test-schedule"
    schedule = call_args[0][1]
    assert schedule.spec.cron_expressions == ["0 9 * * MON-FRI"]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.state.note == "Test schedule"
    assert schedule.state.paused is False
    assert result == {"schedule_id": "test-schedule"}
    mock_dapr_output_client.save_state_object.assert_called_once()


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_schedule_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test create_schedule raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.create_schedule("test-schedule", {}, MockWorkflow)


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_schedule_with_optional_params(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
    mock_dapr_output_client: Mock,
):
    """Test creating a schedule with start_at, end_at, and jitter."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    schedule_args = {
        "cron_expression": "0 9 * * *",
        "workflow_args": {},
        "start_at": "2025-01-01T00:00:00",
        "end_at": "2025-12-31T23:59:59",
        "jitter": 30,
    }

    result = await temporal_client.create_schedule(
        "test-schedule-opts", schedule_args, MockWorkflow
    )

    mock_client.create_schedule.assert_called_once()
    call_args = mock_client.create_schedule.call_args
    schedule = call_args[0][1]
    assert schedule.spec.cron_expressions == ["0 9 * * *"]
    assert schedule.spec.start_at is not None
    assert schedule.spec.start_at.year == 2025
    assert schedule.spec.start_at.month == 1
    assert schedule.spec.end_at is not None
    assert schedule.spec.end_at.year == 2025
    assert schedule.spec.end_at.month == 12
    assert schedule.spec.jitter.total_seconds() == 30
    assert result == {"schedule_id": "test-schedule-opts"}


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_schedule_with_overlap_policy(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
    mock_dapr_output_client: Mock,
):
    """Test creating a schedule with non-default overlap policy."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    schedule_args = {
        "cron_expression": "0 9 * * *",
        "workflow_args": {},
        "overlap_policy": "ALLOW_ALL",
    }

    result = await temporal_client.create_schedule(
        "test-schedule-overlap", schedule_args, MockWorkflow
    )

    mock_client.create_schedule.assert_called_once()
    call_args = mock_client.create_schedule.call_args
    schedule = call_args[0][1]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.ALLOW_ALL
    assert result == {"schedule_id": "test-schedule-overlap"}


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_get_schedule(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test get_schedule returns correctly structured schedule details."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    # Build mock description
    mock_action = MagicMock()
    mock_action.args = [{"workflow_id": "test-wf"}]
    # Make isinstance check work for ScheduleActionStartWorkflow
    from temporalio.client import ScheduleActionStartWorkflow

    mock_action.__class__ = ScheduleActionStartWorkflow

    mock_spec = MagicMock()
    mock_spec.cron_expressions = ["0 9 * * MON-FRI"]

    mock_state = MagicMock()
    mock_state.paused = False
    mock_state.note = "Test note"

    mock_info = MagicMock()
    mock_info.recent_actions = []
    mock_info.next_action_times = []

    mock_schedule = MagicMock()
    mock_schedule.action = mock_action
    mock_schedule.spec = mock_spec
    mock_schedule.state = mock_state

    mock_description = MagicMock()
    mock_description.id = "test-schedule"
    mock_description.schedule = mock_schedule
    mock_description.info = mock_info

    mock_handle = AsyncMock()
    mock_handle.describe.return_value = mock_description
    mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

    result = await temporal_client.get_schedule("test-schedule")

    assert result["schedule_id"] == "test-schedule"
    assert result["cron_expression"] == "0 9 * * MON-FRI"
    assert result["paused"] is False
    assert result["note"] == "Test note"
    assert result["workflow_args"] == {"workflow_id": "test-wf"}
    assert result["recent_actions"] == []
    assert result["next_action_times"] == []


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_get_schedule_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test get_schedule raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.get_schedule("test-schedule")


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_list_schedules(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test list_schedules returns all schedules from Temporal."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    # Create mock schedule list entries
    mock_entry_1 = MagicMock()
    mock_entry_1.id = "schedule-1"
    mock_entry_1.schedule.spec.cron_expressions = ["0 9 * * *"]
    mock_entry_1.schedule.state.paused = False
    mock_entry_1.schedule.state.note = "First"

    mock_entry_2 = MagicMock()
    mock_entry_2.id = "schedule-2"
    mock_entry_2.schedule.spec.cron_expressions = ["0 18 * * *"]
    mock_entry_2.schedule.state.paused = True
    mock_entry_2.schedule.state.note = "Second"

    # Mock async iterator
    async def mock_list_schedules():
        for entry in [mock_entry_1, mock_entry_2]:
            yield entry

    mock_client.list_schedules = MagicMock(return_value=mock_list_schedules())

    result = await temporal_client.list_schedules()

    assert len(result) == 2
    assert result[0]["schedule_id"] == "schedule-1"
    assert result[0]["paused"] is False
    assert result[0]["cron_expression"] == "0 9 * * *"
    assert result[1]["schedule_id"] == "schedule-2"
    assert result[1]["paused"] is True


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_list_schedules_empty(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test list_schedules returns empty list when no schedules exist."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    async def mock_list_schedules():
        return
        yield  # noqa: make it an async generator

    mock_client.list_schedules = MagicMock(return_value=mock_list_schedules())

    result = await temporal_client.list_schedules()
    assert result == []


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_list_schedules_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test list_schedules raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.list_schedules()


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_update_schedule_pause(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test update_schedule pauses a schedule via handle.pause()."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = AsyncMock()
    mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

    result = await temporal_client.update_schedule(
        "test-schedule", {"paused": True, "note": "Maintenance window"}
    )

    mock_handle.pause.assert_called_once_with(note="Maintenance window")
    mock_handle.unpause.assert_not_called()
    mock_handle.update.assert_not_called()
    assert result == {"schedule_id": "test-schedule"}


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_update_schedule_unpause(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test update_schedule unpauses a schedule via handle.unpause()."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = AsyncMock()
    mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

    result = await temporal_client.update_schedule("test-schedule", {"paused": False})

    mock_handle.unpause.assert_called_once_with(note="Unpaused via API")
    mock_handle.pause.assert_not_called()
    assert result == {"schedule_id": "test-schedule"}


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_update_schedule_cron(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test update_schedule changes cron expression via handle.update()."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = AsyncMock()
    mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

    result = await temporal_client.update_schedule(
        "test-schedule", {"cron_expression": "0 10 * * *"}
    )

    mock_handle.update.assert_called_once()
    mock_handle.pause.assert_not_called()
    mock_handle.unpause.assert_not_called()
    assert result == {"schedule_id": "test-schedule"}


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_update_schedule_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test update_schedule raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.update_schedule("test-schedule", {})


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_delete_schedule(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test delete_schedule calls handle.delete()."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = AsyncMock()
    mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

    await temporal_client.delete_schedule("test-schedule")

    mock_client.get_schedule_handle.assert_called_once_with("test-schedule")
    mock_handle.delete.assert_called_once()


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_delete_schedule_client_not_loaded(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test delete_schedule raises ValueError when client is not loaded."""
    with pytest.raises(ValueError, match="Client is not loaded"):
        await temporal_client.delete_schedule("test-schedule")


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_delete_schedule_error(
    mock_connect: AsyncMock,
    temporal_client: TemporalWorkflowClient,
):
    """Test delete_schedule error handling."""
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    await temporal_client.load()

    mock_handle = AsyncMock()
    mock_handle.delete.side_effect = Exception("Schedule not found")
    mock_client.get_schedule_handle = MagicMock(return_value=mock_handle)

    with pytest.raises(Exception, match="Schedule not found"):
        await temporal_client.delete_schedule("nonexistent-schedule")
