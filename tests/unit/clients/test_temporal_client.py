from typing import Any, Dict, Generator, Protocol
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest
from hypothesis import given, settings

from application_sdk.clients.temporal import TemporalClient
from application_sdk.test_utils.hypothesis.strategies.temporal import (
    temporal_connection_params,
    workflow_args,
)


class WorkflowInterface(Protocol):
    @staticmethod
    async def run(*args: Any, **kwargs: Any) -> Dict[str, Any]: ...


@pytest.fixture(scope="module")
def mock_dapr_output_client() -> Generator[MagicMock, None, None]:
    with patch("application_sdk.outputs.statestore.DaprClient") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.__exit__.return_value = None
        yield mock_instance


@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
@given(params=temporal_connection_params())
@settings(deadline=None)
async def test_load(mock_connect: AsyncMock, params: Dict[str, str]):
    temporal_client = TemporalClient(**params)
    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()

    # Verify that Client.connect was called with the correct parameters
    assert any(
        call(
            temporal_client.get_connection_string(),
            namespace=temporal_client.get_namespace(),
        )
        == c
        for c in mock_connect.call_args_list
    )

    # Check that client is set
    assert temporal_client.client == mock_client


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
@given(args=workflow_args())
@settings(deadline=None)
async def test_start_workflow(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    args: Dict[str, Any],
):
    # Create temporal client
    temporal_client = TemporalClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )

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

    # Create a mock workflow class that implements WorkflowInterface
    class MockWorkflow:
        @staticmethod
        async def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "success"}

    # Run start_workflow and capture the result
    result = await temporal_client.start_workflow(args, MockWorkflow)  # type: ignore

    # Assertions
    assert mock_client.start_workflow.called
    assert "workflow_id" in result
    assert result["workflow_id"] == "test_workflow_id"
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
@given(args=workflow_args(include_workflow_id=True))
@settings(deadline=None)
async def test_start_workflow_with_workflow_id(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    args: Dict[str, Any],
):
    # Create temporal client
    temporal_client = TemporalClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )

    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    def start_workflow_side_effect(
        workflow_type: Any, args: Any, id: str, *extra_args: Any, **kwargs: Any
    ) -> MagicMock:
        mock_handle = MagicMock()
        mock_handle.id = id
        mock_handle.result_run_id = "test_run_id"
        return mock_handle

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.side_effect = start_workflow_side_effect

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Create a mock workflow class that implements WorkflowInterface
    class MockWorkflow:
        @staticmethod
        async def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "success"}

    # Run start_workflow and capture the result
    result = await temporal_client.start_workflow(args, MockWorkflow)  # type: ignore

    # Assertions
    assert mock_client.start_workflow.called
    assert "workflow_id" in result
    assert result["workflow_id"] == args["workflow_id"]
    assert result["run_id"] == "test_run_id"


@patch("application_sdk.outputs.secretstore.SecretStoreOutput")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
@given(args=workflow_args())
@settings(deadline=None)
async def test_start_workflow_failure(
    mock_connect: AsyncMock,
    mock_secret_store: MagicMock,
    args: Dict[str, Any],
):
    # Create temporal client
    temporal_client = TemporalClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )

    # Mock the client connection
    mock_client = AsyncMock()
    mock_connect.return_value = mock_client

    # Run load to connect the client
    await temporal_client.load()
    mock_client.start_workflow.side_effect = Exception("Simulated failure")

    # Mock the state store
    mock_secret_store.store_credentials.return_value = "test_credentials"

    # Create a mock workflow class that implements WorkflowInterface
    class MockWorkflow:
        @staticmethod
        async def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "success"}

    # Assertions
    with pytest.raises(Exception, match="Simulated failure"):
        await temporal_client.start_workflow(args, MockWorkflow)  # type: ignore
    assert mock_client.start_workflow.called


@patch("application_sdk.clients.temporal.Worker")
@patch(
    "application_sdk.clients.temporal.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_worker_without_client(
    mock_connect: AsyncMock,
    mock_worker_class: MagicMock,
):
    # Create temporal client
    temporal_client = TemporalClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )

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
):
    # Create temporal client
    temporal_client = TemporalClient(
        host="localhost", port="7233", application_name="test_app", namespace="default"
    )

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
        max_concurrent_activities=ANY,
    )

    assert worker == mock_worker_class.return_value
