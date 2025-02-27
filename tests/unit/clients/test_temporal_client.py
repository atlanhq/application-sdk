from typing import Any, Dict
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck

from application_sdk.clients.temporal import TemporalClient
from application_sdk.workflows import WorkflowInterface
from tests.hypothesis.strategies.temporal import (
    temporal_config_strategy,
    workflow_args_strategy,
    worker_config_strategy
)


@pytest.fixture
def temporal_client():
    return TemporalClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )


@pytest.fixture
def mock_dapr_output_client():
    with patch("application_sdk.outputs.statestore.DaprClient") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.__exit__.return_value = None
        yield mock_instance


@given(config=temporal_config_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_load_property_based(mock_connect: AsyncMock, config: Dict[str, Any]):
    """Property-based test for loading temporal client with various configurations"""
    # Create temporal client with generated config
    temporal_client = TemporalClient(
        host=str(config["host"]),
        port=str(config["port"]),
        application_name=str(config["application_name"]),
        namespace=str(config["namespace"])
    )

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


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_load(mock_connect: AsyncMock, temporal_client: TemporalClient):
    """Test basic loading functionality with fixed configuration"""
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


@given(config=temporal_config_strategy, workflow_args=workflow_args_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow_property_based(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    config: Dict[str, Any],
    workflow_args: Dict[str, Any],
    mock_dapr_output_client: MagicMock,
):
    """Property-based test for starting workflows with various arguments"""
    # Create temporal client with generated config
    temporal_client = TemporalClient(
        host=str(config["host"]),
        port=str(config["port"]),
        application_name=str(config["application_name"]),
        namespace=str(config["namespace"])
    )

    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    mock_handle = MagicMock()
    workflow_id = str(workflow_args.get("workflow_id", "test_workflow_id"))
    mock_handle.id = workflow_id
    mock_handle.result_run_id = "test_run_id"

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.return_value = mock_handle

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Create a mock workflow class
    class MockWorkflow(WorkflowInterface):
        pass

    # Run start_workflow and capture the result
    result = await temporal_client.start_workflow(workflow_args, MockWorkflow)

    # Assertions
    mock_client.start_workflow.assert_called_once()
    mock_dapr_output_client.save_state.assert_called()
    assert "workflow_id" in result
    assert result["workflow_id"] == workflow_id
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_start_workflow_with_workflow_id(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    temporal_client: TemporalClient,
    mock_dapr_output_client,
):
    """Test starting a workflow with a specific workflow ID"""
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    def start_workflow_side_effect(_, __, id, *args, **kwargs):
        mock_handle = MagicMock()
        mock_handle.id = id
        mock_handle.result_run_id = "test_run_id"
        return mock_handle

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.side_effect = start_workflow_side_effect

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Sample workflow arguments with specific workflow ID
    credentials = {"username": "test_username", "password": "test_password"}
    workflow_args = {
        "param1": "value1",
        "credentials": credentials,
        "workflow_id": "test_workflow_id",
    }

    # Create a mock workflow class
    class MockWorkflow(WorkflowInterface):
        pass

    # Run start_workflow and capture the result
    result = await temporal_client.start_workflow(workflow_args, MockWorkflow)

    # Assertions
    mock_client.start_workflow.assert_called_once()
    mock_dapr_output_client.save_state.assert_called()
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
    temporal_client: TemporalClient,
    mock_dapr_output_client,
):
    """Test handling of workflow start failures"""
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

    # Create a mock workflow class
    class MockWorkflow(WorkflowInterface):
        pass

    # Assertions
    with pytest.raises(Exception, match="Simulated failure"):
        await temporal_client.start_workflow(workflow_args, MockWorkflow)
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
    temporal_client: TemporalClient,
):
    """Test worker creation fails when client is not loaded"""
    # Mock workflow class and activities
    class MockWorkflow(WorkflowInterface):
        pass

    def mock_activity():
        pass

    workflow_classes = [MockWorkflow]
    activities = [mock_activity]
    passthrough_modules = ["application_sdk", "os"]

    # Run create_worker without loading client first
    with pytest.raises(ValueError, match="Client is not loaded"):
        temporal_client.create_worker(activities, workflow_classes, passthrough_modules)


@given(config=temporal_config_strategy, worker_config=worker_config_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@patch("application_sdk.clients.temporal.Worker")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_worker_property_based(
    mock_connect: AsyncMock,
    mock_worker_class: MagicMock,
    config: Dict[str, Any],
    worker_config: Dict[str, Any],
):
    """Property-based test for creating workers with various configurations"""
    # Create temporal client with generated config
    temporal_client = TemporalClient(
        host=str(config["host"]),
        port=str(config["port"]),
        application_name=str(config["application_name"]),
        namespace=str(config["namespace"])
    )

    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()

    # Mock workflow class and activities
    class MockWorkflow(WorkflowInterface):
        pass

    def mock_activity():
        pass

    workflow_classes = [MockWorkflow]
    activities = [mock_activity]
    passthrough_modules = ["application_sdk", "os"]

    # Update temporal client with worker config
    for key, value in worker_config.items():
        setattr(temporal_client, key, value)

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
        max_concurrent_activities=worker_config["max_concurrent_activities"],
    )

    assert worker == mock_worker_class.return_value
