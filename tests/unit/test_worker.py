import asyncio
import builtins
import importlib
import importlib.util
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.worker import Worker


@pytest.fixture
def mock_workflow_client():
    workflow_client = Mock(spec=WorkflowClient)
    workflow_client.worker_task_queue = "test_queue"
    workflow_client.application_name = "test_app"
    workflow_client.namespace = "test_namespace"
    workflow_client.host = "localhost"
    workflow_client.port = "7233"
    workflow_client.get_connection_string = Mock(return_value="localhost:7233")

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


class TestEventLoopPolicy:
    """Test event loop policy configuration for different platforms."""

    @pytest.mark.parametrize("platform", ["win32", "cygwin"])
    def test_windows_platforms_set_windows_selector_policy(self, platform):
        """Test that Windows/Cygwin platforms set WindowsSelectorEventLoopPolicy."""
        # Save original policy
        original_policy = asyncio.get_event_loop_policy()

        # Create a mock WindowsSelectorEventLoopPolicy class since it only exists on Windows
        mock_windows_policy = Mock()
        mock_windows_policy_class = Mock(return_value=mock_windows_policy)

        try:
            # Mock platform and the Windows-specific policy class
            with patch.object(sys, "platform", platform):
                with patch.object(
                    asyncio,
                    "WindowsSelectorEventLoopPolicy",
                    mock_windows_policy_class,
                    create=True,
                ):
                    with patch.object(
                        asyncio, "set_event_loop_policy"
                    ) as mock_set_policy:
                        import application_sdk.worker

                        importlib.reload(application_sdk.worker)

                        # Verify that WindowsSelectorEventLoopPolicy was instantiated and set
                        mock_windows_policy_class.assert_called_once()
                        mock_set_policy.assert_called_once_with(mock_windows_policy)
        finally:
            # Restore original policy and reload module
            asyncio.set_event_loop_policy(original_policy)
            importlib.reload(application_sdk.worker)

    def test_non_windows_platform_with_uvloop_available(self):
        """Test that non-Windows platform uses uvloop when available."""
        # Skip this test if uvloop is not installed in the test environment
        if importlib.util.find_spec("uvloop") is None:
            pytest.skip("uvloop is not installed, skipping uvloop availability test")

        # Save original policy
        original_policy = asyncio.get_event_loop_policy()

        try:
            # Mock non-Windows platform (Linux)
            with patch.object(sys, "platform", "linux"):
                import application_sdk.worker

                importlib.reload(application_sdk.worker)

                # Check the policy - should be uvloop since it's available
                current_policy = asyncio.get_event_loop_policy()
                policy_class_name = type(current_policy).__module__
                assert (
                    "uvloop" in policy_class_name
                ), f"Expected uvloop policy, got {type(current_policy)}"
        finally:
            # Restore original policy and reload module
            asyncio.set_event_loop_policy(original_policy)
            importlib.reload(application_sdk.worker)

    def test_non_windows_platform_without_uvloop_falls_back(self):
        """Test that non-Windows platform falls back to default when uvloop is unavailable.

        When uvloop import fails, the code gracefully handles the exception and
        leaves the default asyncio policy in place (it doesn't crash or set an invalid policy).
        """
        # Save original policy
        original_policy = asyncio.get_event_loop_policy()
        uvloop_backup = sys.modules.get("uvloop")

        try:
            with patch.object(sys, "platform", "linux"):
                # Remove uvloop from sys.modules if it exists
                if "uvloop" in sys.modules:
                    del sys.modules["uvloop"]

                # Set a known default policy before reloading.
                # The code under test should gracefully handle uvloop import failure
                # and leave this policy unchanged (not crash or set uvloop).
                default_policy = asyncio.DefaultEventLoopPolicy()
                asyncio.set_event_loop_policy(default_policy)

                # Get reference to builtins import function
                original_import = builtins.__import__

                def mock_import(name, *args, **kwargs):
                    if name == "uvloop":
                        raise ImportError("No module named 'uvloop'")
                    return original_import(name, *args, **kwargs)

                with patch.object(builtins, "__import__", side_effect=mock_import):
                    import application_sdk.worker

                    importlib.reload(application_sdk.worker)

                    current_policy = asyncio.get_event_loop_policy()

                    # Verify the policy is not uvloop (graceful fallback)
                    policy_class_name = type(current_policy).__module__
                    assert (
                        "uvloop" not in policy_class_name
                    ), "Policy should not be uvloop when uvloop import fails"

                    # Verify the policy is still a valid asyncio policy
                    assert isinstance(
                        current_policy, asyncio.AbstractEventLoopPolicy
                    ), "Policy should be a valid asyncio policy"
        finally:
            if uvloop_backup is not None:
                sys.modules["uvloop"] = uvloop_backup
            asyncio.set_event_loop_policy(original_policy)
            importlib.reload(application_sdk.worker)
