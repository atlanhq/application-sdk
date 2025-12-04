"""Simplified unit tests for the activities module."""

from datetime import datetime, timedelta
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.handlers import HandlerInterface
from application_sdk.services.statestore import StateType


class MockHandler(HandlerInterface):
    """Mock handler for testing."""

    async def preflight_check(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Mock preflight check."""
        return {"status": "success"}

    async def fetch_metadata(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Mock fetch metadata."""
        return {"metadata": "test"}

    async def load(self, config: Dict[str, Any]) -> None:
        """Mock load method."""
        pass

    async def test_auth(self, config: Dict[str, Any]) -> bool:
        """Mock test auth method."""
        return True


class MockActivities(ActivitiesInterface[ActivitiesState[MockHandler]]):
    """Mock activities implementation for testing."""

    def __init__(self):
        """Initialize with a fixed workflow ID for testing."""
        super().__init__()
        self.test_workflow_id = "test-workflow-123"

    def _get_test_workflow_id(self) -> str:
        """Get a test workflow ID for unit testing."""
        return self.test_workflow_id

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Override to use test workflow ID."""
        workflow_id = self._get_test_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = ActivitiesState()
        self._state[workflow_id].workflow_args = workflow_args

    async def _get_state(self, workflow_args: Dict[str, Any]):
        """Override to use test workflow ID."""
        workflow_id = self._get_test_workflow_id()
        if workflow_id not in self._state:
            await self._set_state(workflow_args)
        return self._state[workflow_id]

    async def _clean_state(self):
        """Override to use test workflow ID."""
        workflow_id = self._get_test_workflow_id()
        if workflow_id in self._state:
            self._state.pop(workflow_id)

    async def test_activity(self, workflow_args: Dict[str, Any]) -> None:
        """Test activity method."""
        state = await self._get_state(workflow_args)
        state.handler = MockHandler()
        await state.handler.preflight_check({"test": "config"})


@pytest.fixture
def mock_activities():
    """Create a mock activities instance."""
    return MockActivities()


@pytest.fixture
def sample_workflow_args():
    """Sample workflow arguments."""
    return {
        "workflow_id": "test-workflow-123",
        "metadata": {"key": "value"},
        "config": {"setting": "test"},
    }


class TestActivitiesState:
    """Test cases for ActivitiesState."""

    def test_activities_state_initialization(self):
        """Test ActivitiesState initialization."""
        state = ActivitiesState[MockHandler]()
        assert state.handler is None
        assert state.workflow_args is None

    def test_activities_state_with_values(self):
        """Test ActivitiesState with values."""
        handler = MockHandler()
        workflow_args = {"test": "data"}

        state = ActivitiesState[MockHandler](
            handler=handler, workflow_args=workflow_args
        )

        assert state.handler == handler
        assert state.workflow_args == workflow_args


class TestActivitiesInterface:
    """Test cases for ActivitiesInterface."""

    async def test_set_state_new_workflow(self, mock_activities):
        """Test setting state for a new workflow."""
        workflow_args = {"test": "data"}

        await mock_activities._set_state(workflow_args)

        assert "test-workflow-123" in mock_activities._state
        assert (
            mock_activities._state["test-workflow-123"].workflow_args == workflow_args
        )

    async def test_set_state_existing_workflow(self, mock_activities):
        """Test setting state for an existing workflow."""
        workflow_args1 = {"test": "data1"}
        workflow_args2 = {"test": "data2"}

        # Set state twice for the same workflow
        await mock_activities._set_state(workflow_args1)
        await mock_activities._set_state(workflow_args2)

        assert "test-workflow-123" in mock_activities._state
        assert (
            mock_activities._state["test-workflow-123"].workflow_args == workflow_args2
        )

    async def test_get_state_existing_workflow(self, mock_activities):
        """Test getting state for an existing workflow."""
        workflow_args = {"test": "data"}

        # Set state first
        await mock_activities._set_state(workflow_args)

        # Get state
        state = await mock_activities._get_state(workflow_args)

        assert state.workflow_args == workflow_args

    async def test_get_state_new_workflow(self, mock_activities):
        """Test getting state for a new workflow (should auto-initialize)."""
        workflow_args = {"test": "data"}

        # Get state without setting it first
        state = await mock_activities._get_state(workflow_args)

        assert state.workflow_args == workflow_args
        assert "test-workflow-123" in mock_activities._state

    async def test_clean_state(self, mock_activities):
        """Test cleaning state for a workflow."""
        workflow_args = {"test": "data"}

        # Set state first
        await mock_activities._set_state(workflow_args)
        assert "test-workflow-123" in mock_activities._state

        # Clean state
        await mock_activities._clean_state()
        assert "test-workflow-123" not in mock_activities._state

    async def test_clean_state_nonexistent_workflow(self, mock_activities):
        """Test cleaning state for a non-existent workflow."""
        # Clean state for workflow that doesn't exist
        await mock_activities._clean_state()
        # Should not raise any exception


class TestActivitiesInterfaceActivities:
    """Test cases for ActivitiesInterface activity methods."""

    @patch("application_sdk.activities.build_output_path")
    @patch("application_sdk.activities.get_workflow_run_id")
    @patch("application_sdk.activities.get_workflow_id")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    async def test_get_workflow_args_success(
        self,
        mock_get_state,
        mock_get_workflow_id,
        mock_get_workflow_run_id,
        mock_build_output_path,
        mock_activities,
    ):
        """Test successful get_workflow_args activity."""
        workflow_config = {"workflow_id": "test-123"}
        expected_config = {"workflow_id": "test-123", "config": "data"}
        mock_get_state.return_value = expected_config
        mock_get_workflow_id.return_value = "test-123"
        mock_get_workflow_run_id.return_value = "run-456"
        mock_build_output_path.return_value = "test/output/path"

        result = await mock_activities.get_workflow_args(workflow_config)

        # The result should include the output_prefix and output_path added by get_workflow_args
        assert result["workflow_id"] == expected_config["workflow_id"]
        assert result["config"] == expected_config["config"]
        assert "output_prefix" in result
        assert "output_path" in result

        mock_get_state.assert_called_once_with("test-123", StateType.WORKFLOWS)

    @patch("application_sdk.activities.build_output_path")
    @patch("application_sdk.activities.get_workflow_run_id")
    @patch("application_sdk.activities.get_workflow_id")
    async def test_get_workflow_args_missing_workflow_id(
        self,
        mock_get_workflow_id,
        mock_get_workflow_run_id,
        mock_build_output_path,
        mock_activities,
    ):
        """Test get_workflow_args with missing workflow_id."""
        workflow_config = {"other_param": "value"}
        mock_get_workflow_id.side_effect = Exception("Failed to get workflow id")

        with pytest.raises(Exception, match="Failed to get workflow id"):
            await mock_activities.get_workflow_args(workflow_config)

    @patch("application_sdk.activities.build_output_path")
    @patch("application_sdk.activities.get_workflow_run_id")
    @patch("application_sdk.activities.get_workflow_id")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    async def test_get_workflow_args_extraction_error(
        self,
        mock_get_state,
        mock_get_workflow_id,
        mock_get_workflow_run_id,
        mock_build_output_path,
        mock_activities,
    ):
        """Test get_workflow_args when extraction fails."""
        workflow_config = {"workflow_id": "test-123"}
        mock_get_state.side_effect = Exception("Extraction failed")
        mock_get_workflow_id.return_value = "test-123"
        mock_get_workflow_run_id.return_value = "run-456"
        mock_build_output_path.return_value = "test/output/path"

        with pytest.raises(Exception, match="Extraction failed"):
            await mock_activities.get_workflow_args(workflow_config)

    async def test_preflight_check_success(self, mock_activities):
        """Test successful preflight_check activity."""
        workflow_args = {"metadata": {"test": "config"}}

        # Set up state with handler
        await mock_activities._set_state(workflow_args)
        state = await mock_activities._get_state(workflow_args)
        state.handler = MockHandler()

        result = await mock_activities.preflight_check(workflow_args)

        assert result == {"status": "success"}

    async def test_preflight_check_no_handler(self, mock_activities):
        """Test preflight_check when handler is not found."""
        workflow_args = {"metadata": {"test": "config"}}

        with pytest.raises(ValueError, match="Preflight check handler not found"):
            await mock_activities.preflight_check(workflow_args)

    async def test_preflight_check_handler_failure(self, mock_activities):
        """Test preflight_check when handler fails."""
        workflow_args = {"metadata": {"test": "config"}}

        # Set up state with failing handler
        await mock_activities._set_state(workflow_args)
        state = await mock_activities._get_state(workflow_args)

        mock_handler = MockHandler()
        mock_handler.preflight_check = AsyncMock(
            return_value={"error": "Handler failed"}
        )
        state.handler = mock_handler

        with pytest.raises(ValueError, match="Preflight check failed"):
            await mock_activities.preflight_check(workflow_args)


class TestMockActivities:
    """Test cases for the MockActivities implementation."""

    async def test_test_activity(self, mock_activities):
        """Test the test_activity method."""
        workflow_args = {"test": "data"}

        await mock_activities.test_activity(workflow_args)

        # Verify state was set up correctly
        state = await mock_activities._get_state(workflow_args)
        assert state.handler is not None
        assert isinstance(state.handler, MockHandler)


class MockCloseableResource:
    """Mock resource that can be closed, simulating SQL client or similar."""

    def __init__(self, resource_id: str):
        """Initialize with a unique ID for tracking."""
        self.resource_id = resource_id
        self.closed = False

    async def close(self) -> None:
        """Mark the resource as closed."""
        self.closed = True


class StateRefreshActivities(ActivitiesInterface[ActivitiesState[MockHandler]]):
    """Activities implementation for testing state refresh with closeable resources."""

    def __init__(self):
        """Initialize activities for state refresh testing."""
        super().__init__()
        self.resource_counter = 0

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Override to create closeable resources for testing.

        This simulates the behavior of SQL activities where old resources
        (like SQL clients) are closed before creating new ones during refresh.
        """
        from application_sdk.activities.common.utils import get_workflow_id

        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = ActivitiesState()

        # Close existing resource if it exists (simulating SQL client cleanup)
        existing_state = self._state.get(workflow_id)
        if existing_state and hasattr(existing_state, "test_resource"):
            old_resource = existing_state.test_resource  # type: ignore
            if old_resource and not old_resource.closed:
                await old_resource.close()

        # Call parent to set workflow_args and timestamp
        await super()._set_state(workflow_args)

        # Create a new closeable resource (simulating SQL client)
        self.resource_counter += 1
        resource = MockCloseableResource(f"resource-{self.resource_counter}")
        # Store resource in a custom attribute for testing
        self._state[workflow_id].test_resource = resource  # type: ignore


class TestStateRefresh:
    """Test cases for 15-minute state refresh functionality."""

    @patch("application_sdk.activities.get_workflow_id")
    async def test_state_refresh_after_15_minutes_closes_old_resource(
        self, mock_get_workflow_id
    ):
        """Test that state refresh after 15 minutes closes old resources and creates new ones."""
        mock_get_workflow_id.return_value = "test-workflow-refresh"
        activities = StateRefreshActivities()
        workflow_args = {"test": "data"}

        # Set initial time
        initial_time = datetime(2024, 1, 1, 12, 0, 0)

        # Create initial state
        with patch("application_sdk.activities.datetime") as mock_datetime_module:
            # Mock datetime.now() to return our initial time
            mock_datetime_module.now.return_value = initial_time
            # Allow datetime() constructor to work normally
            mock_datetime_module.side_effect = lambda *args, **kw: datetime(*args, **kw)

            # Call _get_state to create initial state
            state1 = await activities._get_state(workflow_args)
            initial_resource = state1.test_resource  # type: ignore
            assert initial_resource is not None
            assert not initial_resource.closed
            assert state1.last_updated_timestamp == initial_time

        # Simulate 15+ minutes passing
        stale_time = initial_time + timedelta(minutes=16)

        # Call _get_state again - should trigger refresh
        with patch("application_sdk.activities.datetime") as mock_datetime_module:
            # Mock datetime.now() to return stale time
            mock_datetime_module.now.return_value = stale_time
            # Allow datetime() constructor to work normally
            mock_datetime_module.side_effect = lambda *args, **kw: datetime(*args, **kw)

            state2 = await activities._get_state(workflow_args)
            new_resource = state2.test_resource  # type: ignore

            # Verify old resource was closed
            assert (
                initial_resource.closed
            ), "Old resource should be closed during refresh"

            # Verify new resource was created
            assert new_resource is not None
            assert new_resource.resource_id != initial_resource.resource_id
            assert not new_resource.closed

            # Verify timestamp was updated
            assert state2.last_updated_timestamp == stale_time
            assert state2.last_updated_timestamp != initial_time

    @patch("application_sdk.activities.get_workflow_id")
    async def test_state_no_refresh_before_15_minutes(self, mock_get_workflow_id):
        """Test that state is not refreshed if less than 15 minutes have passed."""
        mock_get_workflow_id.return_value = "test-workflow-no-refresh"
        activities = StateRefreshActivities()
        workflow_args = {"test": "data"}

        # Set initial time
        initial_time = datetime(2024, 1, 1, 12, 0, 0)

        # Create initial state
        with patch("application_sdk.activities.datetime") as mock_datetime:
            mock_datetime.now.return_value = initial_time
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            state1 = await activities._get_state(workflow_args)
            initial_resource = state1.test_resource  # type: ignore
            initial_timestamp = state1.last_updated_timestamp

        # Simulate less than 15 minutes passing
        recent_time = initial_time + timedelta(minutes=10)

        # Call _get_state again - should NOT trigger refresh
        with patch("application_sdk.activities.datetime") as mock_datetime:
            mock_datetime.now.return_value = recent_time
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            state2 = await activities._get_state(workflow_args)
            same_resource = state2.test_resource  # type: ignore

            # Verify old resource was NOT closed
            assert (
                not initial_resource.closed
            ), "Resource should not be closed before 15 minutes"

            # Verify same resource is still in use
            assert same_resource.resource_id == initial_resource.resource_id

            # Verify timestamp was NOT updated
            assert state2.last_updated_timestamp == initial_timestamp

    @patch("application_sdk.activities.get_workflow_id")
    async def test_state_refresh_sets_timestamp_on_initial_creation(
        self, mock_get_workflow_id
    ):
        """Test that _set_state sets last_updated_timestamp on initial state creation."""
        mock_get_workflow_id.return_value = "test-workflow-timestamp"
        activities = StateRefreshActivities()
        workflow_args = {"test": "data"}

        test_time = datetime(2024, 1, 1, 12, 0, 0)

        with patch("application_sdk.activities.datetime") as mock_datetime:
            mock_datetime.now.return_value = test_time
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            # Create initial state
            state = await activities._get_state(workflow_args)

            # Verify timestamp was set
            assert state.last_updated_timestamp is not None
            assert state.last_updated_timestamp == test_time

    @patch("application_sdk.activities.get_workflow_id")
    async def test_state_refresh_with_sql_client_simulation(self, mock_get_workflow_id):
        """Test state refresh scenario simulating SQL client behavior."""
        mock_get_workflow_id.return_value = "test-workflow-sql-sim"
        activities = StateRefreshActivities()
        workflow_args = {"credential_guid": "test-cred-123", "test": "data"}

        initial_time = datetime(2024, 1, 1, 12, 0, 0)

        # Create initial state with resource
        with patch("application_sdk.activities.datetime") as mock_datetime:
            mock_datetime.now.return_value = initial_time
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            state1 = await activities._get_state(workflow_args)
            old_resource = state1.test_resource  # type: ignore
            old_resource_id = old_resource.resource_id

        # Simulate 20 minutes passing (well beyond 15-minute threshold)
        stale_time = initial_time + timedelta(minutes=20)

        # Trigger refresh
        with patch("application_sdk.activities.datetime") as mock_datetime:
            mock_datetime.now.return_value = stale_time
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            state2 = await activities._get_state(workflow_args)
            new_resource = state2.test_resource  # type: ignore

            # Verify old resource was properly closed
            assert old_resource.closed, "Old SQL client should be closed during refresh"

            # Verify new resource was created
            assert new_resource.resource_id != old_resource_id
            assert new_resource.resource_id == "resource-2"  # Second resource created

            # Verify state was refreshed
            assert state2.last_updated_timestamp == stale_time
            assert state2.workflow_args == workflow_args
