from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)


@pytest.fixture
def temporal_config():
    return TemporalConfig(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )


@pytest.fixture
def temporal_resource(temporal_config: TemporalConfig):
    return TemporalResource(temporal_config=temporal_config)


@patch(
    "application_sdk.workflows.resources.temporal_resource.Client.connect",
    new_callable=AsyncMock,
)
async def test_load(mock_connect: AsyncMock, temporal_resource: TemporalResource):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_resource.load()

    # Verify that Client.connect was called with the correct parameters
    mock_connect.assert_called_once_with(
        temporal_resource.config.get_connection_string(),
        namespace=temporal_resource.config.get_namespace(),
    )

    # Check that client is set
    assert temporal_resource.client == mock_client


@patch("application_sdk.workflows.resources.temporal_resource.StateStore")
@patch(
    "application_sdk.workflows.resources.temporal_resource.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow(
    mock_connect: AsyncMock,
    mock_state_store: MagicMock,
    temporal_resource: TemporalResource,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    mock_handle = MagicMock()
    mock_handle.id = "test_workflow_id"
    mock_handle.result_run_id = "test_run_id"

    # Run load to connect the client
    await temporal_resource.load()
    mock_client.start_workflow.return_value = mock_handle

    # Mock the state store
    mock_state_store = MagicMock()
    mock_state_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {"param1": "value1", "credentials": credentials}
    workflow_class = MagicMock()  # Mocking the workflow class

    # Run start_workflow and capture the result
    result = await temporal_resource.start_workflow(workflow_args, workflow_class)

    # Assertions
    mock_client.start_workflow.assert_called_once()
    assert "workflow_id" in result
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.workflows.resources.temporal_resource.StateStore")
@patch(
    "application_sdk.workflows.resources.temporal_resource.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow_with_workflow_id(
    mock_connect: AsyncMock,
    mock_state_store: MagicMock,
    temporal_resource: TemporalResource,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    def start_workflow_side_effect(_, __, id, *args, **kwargs):
        mock_handle = MagicMock()
        mock_handle.id = id
        mock_handle.result_run_id = "test_run_id"
        return mock_handle

    # Run load to connect the client
    await temporal_resource.load()
    mock_client.start_workflow.side_effect = start_workflow_side_effect

    # Mock the state store
    mock_state_store = MagicMock()
    mock_state_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {
        "param1": "value1",
        "credentials": credentials,
        "workflow_id": "test_workflow_id",
    }
    workflow_class = MagicMock()  # Mocking the workflow class

    # Run start_workflow and capture the result
    result = await temporal_resource.start_workflow(
        workflow_args,
        workflow_class,
    )

    # Assertions
    mock_client.start_workflow.assert_called_once()
    assert "workflow_id" in result
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.workflows.resources.temporal_resource.StateStore")
@patch(
    "application_sdk.workflows.resources.temporal_resource.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow_failure(
    mock_connect: AsyncMock,
    mock_state_store: MagicMock,
    temporal_resource: TemporalResource,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_resource.load()
    mock_client.start_workflow.side_effect = Exception("Simulated failure")

    # Mock the state store
    mock_state_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {"param1": "value1", "credentials": credentials}
    workflow_class = MagicMock()  # Mocking the workflow class

    # Assertions
    with pytest.raises(Exception, match="Simulated failure"):
        await temporal_resource.start_workflow(workflow_args, workflow_class)
    mock_client.start_workflow.assert_called_once()


@patch("application_sdk.workflows.resources.temporal_resource.Worker")
@patch(
    "application_sdk.workflows.resources.temporal_resource.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_worker_without_client(
    mock_connect: AsyncMock,
    mock_worker_class: MagicMock,
    temporal_resource: TemporalResource,
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
        temporal_resource.create_worker(
            activities, workflow_classes, passthrough_modules
        )


@patch("application_sdk.workflows.resources.temporal_resource.Worker")
@patch(
    "application_sdk.workflows.resources.temporal_resource.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_worker(
    mock_connect: AsyncMock,
    mock_worker_class: MagicMock,
    temporal_resource: TemporalResource,
):
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_resource.load()

    # Mock workflow class and activities
    workflow_classes = [MagicMock(), MagicMock()]
    activities = [MagicMock(), MagicMock()]
    passthrough_modules = ["application_sdk", "os"]

    # Run create_worker
    worker = temporal_resource.create_worker(
        activities, workflow_classes, passthrough_modules
    )

    # Verify Worker was instantiated with the expected parameters
    mock_worker_class.assert_called_once_with(
        temporal_resource.client,
        task_queue=temporal_resource.worker_task_queue,
        workflows=workflow_classes,
        activities=activities,
        workflow_runner=ANY,
        interceptors=ANY,
    )

    assert worker == mock_worker_class.return_value
