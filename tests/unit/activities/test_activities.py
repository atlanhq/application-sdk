"""Simplified unit tests for the activities module."""

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.handlers import HandlerInterface


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

    @patch("application_sdk.inputs.statestore.StateStoreInput.extract_configuration")
    async def test_get_workflow_args_success(
        self, mock_extract_config, mock_activities
    ):
        """Test successful get_workflow_args activity."""
        workflow_config = {"workflow_id": "test-123"}
        expected_config = {"workflow_id": "test-123", "config": "data"}
        mock_extract_config.return_value = expected_config

        result = await mock_activities.get_workflow_args(workflow_config)

        assert result == expected_config
        mock_extract_config.assert_called_once_with("test-123")

    async def test_get_workflow_args_missing_workflow_id(self, mock_activities):
        """Test get_workflow_args with missing workflow_id."""
        workflow_config = {"other_param": "value"}

        with pytest.raises(ValueError, match="workflow_id is required"):
            await mock_activities.get_workflow_args(workflow_config)

    @patch("application_sdk.inputs.statestore.StateStoreInput.extract_configuration")
    async def test_get_workflow_args_extraction_error(
        self, mock_extract_config, mock_activities
    ):
        """Test get_workflow_args when extraction fails."""
        workflow_config = {"workflow_id": "test-123"}
        mock_extract_config.side_effect = Exception("Extraction failed")

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
