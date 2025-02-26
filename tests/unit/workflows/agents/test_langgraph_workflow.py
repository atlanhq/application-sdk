from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio import workflow

# Import AgentState directly
from application_sdk.agents import AgentState


@pytest.fixture
def mock_activity_class():
    """Create a mock LangGraphActivities class."""
    mock_cls = MagicMock()
    mock_cls.run_agent = AsyncMock(return_value={"result": "test_result"})
    return mock_cls


@pytest.fixture
def workflow_with_mock_activities(mock_activity_class):
    """Create a LangGraphWorkflow instance with mock activities."""
    # Import locally to avoid circular import
    from application_sdk.agents import LangGraphWorkflow

    # Create a subclass that overrides the activities_cls
    class TestWorkflow(LangGraphWorkflow):
        activities_cls = mock_activity_class

    return TestWorkflow()


def test_init():
    """Test initialization of LangGraphWorkflow."""
    # Import locally to avoid circular import
    from application_sdk.agents import LangGraphWorkflow

    workflow_instance = LangGraphWorkflow()

    # Check that the state is initialized correctly
    assert isinstance(workflow_instance.state, dict)
    assert "messages" in workflow_instance.state
    assert workflow_instance.state["messages"] == []


def test_get_activities():
    """Test get_activities method."""
    # Import locally to avoid circular import
    from application_sdk.agents import LangGraphWorkflow

    # Create a mock activities instance
    mock_activities = MagicMock()

    # Get the activities
    activities = LangGraphWorkflow.get_activities(mock_activities)

    # Should return a list containing the run_agent method
    assert len(activities) == 1
    assert activities[0] == mock_activities.run_agent


@pytest.mark.asyncio
async def test_run_without_user_query(workflow_with_mock_activities):
    """Test running the workflow without a user query."""
    # Create workflow input with no user_query
    workflow_input = {
        "graph_builder_name": "test_builder",
    }

    # Execute the workflow
    result = await workflow_with_mock_activities.run(workflow_input)

    # Should return error
    assert "error" in result
    assert "No user query provided" in result["error"]


@pytest.mark.asyncio
async def test_run_without_graph_builder(workflow_with_mock_activities):
    """Test running the workflow without a graph builder name."""
    # Create workflow input with no graph_builder_name
    workflow_input = {
        "user_query": "test query",
    }

    # Execute the workflow
    result = await workflow_with_mock_activities.run(workflow_input)

    # Should return error
    assert "error" in result
    assert "No graph builder name provided" in result["error"]


@pytest.mark.asyncio
@patch.object(workflow, "execute_activity_method")
async def test_run_with_valid_input(
    mock_execute_activity, workflow_with_mock_activities
):
    """Test running the workflow with valid input."""
    # Create workflow input
    workflow_input = {
        "user_query": "test query",
        "graph_builder_name": "test_builder",
    }

    # Set up mock to return a result
    mock_execute_activity.return_value = {"result": "test_result"}

    # Execute the workflow
    result = await workflow_with_mock_activities.run(workflow_input)

    # Check that execute_activity_method was called correctly
    mock_execute_activity.assert_called_once()

    # Access args and kwargs correctly based on actual call pattern
    args, kwargs = mock_execute_activity.call_args

    # Check that the first arg is the activity method
    assert args[0] == workflow_with_mock_activities.activities_cls.run_agent

    # Check activity_input is passed as first element in the args list
    assert "args" in kwargs
    activity_input = kwargs["args"][0]
    assert activity_input["user_query"] == "test query"
    assert activity_input["graph_builder_name"] == "test_builder"
    assert "state" in activity_input

    # Check return value
    assert result == {"result": "test_result"}


@pytest.mark.asyncio
@patch.object(workflow, "execute_activity_method")
async def test_run_with_state(mock_execute_activity, workflow_with_mock_activities):
    """Test running the workflow with initial state."""
    # Create workflow input with state
    initial_state = AgentState(messages=[])
    workflow_input = {
        "user_query": "test query",
        "graph_builder_name": "test_builder",
        "state": initial_state,
    }

    # Set up mock to return a result
    mock_execute_activity.return_value = {"result": "test_result"}

    # Execute the workflow
    await workflow_with_mock_activities.run(workflow_input)

    # Check that the workflow state was updated
    assert workflow_with_mock_activities.state == initial_state

    # Check that the activity was called with the correct state
    args, kwargs = mock_execute_activity.call_args

    # Access activity_input from kwargs["args"]
    assert "args" in kwargs
    activity_input = kwargs["args"][0]
    assert activity_input["state"] == initial_state
