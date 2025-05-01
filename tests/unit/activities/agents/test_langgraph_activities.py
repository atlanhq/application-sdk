from unittest.mock import MagicMock, patch

import pytest

from application_sdk.activities.agents.langgraph import (
    get_graph_builder,
    register_graph_builder,
)


@pytest.fixture
def mock_state_graph():
    """Create a mock StateGraph for testing."""
    return MagicMock()


@pytest.fixture
def mock_graph_builder(mock_state_graph):
    """Create a mock graph builder function."""
    builder = MagicMock(return_value=mock_state_graph)
    return builder


@pytest.fixture
def mock_langgraph_agent():
    """Create a mock LangGraphAgent."""
    agent = MagicMock()
    agent.state = {"messages": [], "result": "test_result"}
    return agent


def test_register_graph_builder(mock_graph_builder):
    """Test registering a graph builder function."""
    # First clear any existing builders
    from application_sdk.activities.agents.langgraph import _graph_builders

    _graph_builders.clear()

    # Register a builder
    register_graph_builder("test_builder", mock_graph_builder)

    # Check it was registered
    assert "test_builder" in _graph_builders
    assert _graph_builders["test_builder"] == mock_graph_builder


def test_get_graph_builder_direct(mock_graph_builder):
    """Test getting a graph builder directly from registry."""
    # First register a builder
    register_graph_builder("test_builder", mock_graph_builder)

    # Get the builder
    builder = get_graph_builder("test_builder")

    assert builder == mock_graph_builder


@patch("importlib.import_module")
def test_get_graph_builder_import(mock_import, mock_graph_builder):
    """Test getting a graph builder by importing it."""
    # Set up the mock module
    mock_module = MagicMock()
    mock_module.test_func = mock_graph_builder
    mock_import.return_value = mock_module

    # Get the builder
    builder = get_graph_builder("test_module.test_func")

    # Check it was imported and cached
    mock_import.assert_called_once_with("test_module")
    assert builder == mock_graph_builder

    # Check it was cached
    from application_sdk.activities.agents.langgraph import _graph_builders

    assert "test_module.test_func" in _graph_builders


@patch("importlib.import_module")
def test_get_graph_builder_import_error(mock_import):
    """Test handling import error when getting a graph builder."""
    mock_import.side_effect = ImportError("Test error")

    # Should return None on error
    builder = get_graph_builder("nonexistent.module")

    assert builder is None


@patch("importlib.import_module")
def test_get_graph_builder_attribute_error(mock_import):
    """Test handling attribute error when getting a graph builder."""
    # Set up the mock module without the requested attribute
    mock_module = MagicMock(spec=[])
    mock_import.return_value = mock_module

    # Should return None when attribute doesn't exist
    builder = get_graph_builder("test_module.nonexistent_func")

    assert builder is None


@pytest.fixture
def activities():
    """Create a LangGraphActivities instance for testing."""
    # Import here to avoid circular import
    from application_sdk.activities.agents.langgraph import LangGraphActivities

    return LangGraphActivities()


@pytest.mark.asyncio
@patch("application_sdk.activities.agents.langgraph.get_graph_builder")
@patch("application_sdk.activities.agents.langgraph.LangGraphAgent")
async def test_run_agent_success(
    mock_agent_class, mock_get_builder, activities, mock_graph_builder, mock_state_graph
):
    """Test successfully running an agent."""
    # Set up mocks
    mock_get_builder.return_value = mock_graph_builder
    mock_agent_instance = MagicMock()
    mock_agent_instance.state = {"key": "value"}
    mock_agent_class.return_value = mock_agent_instance

    # Create input
    activity_input = {
        "user_query": "test query",
        "graph_builder_name": "test_builder",
        "state": {"initial": "state"},
    }

    # Run the activity
    result = await activities.run_agent(activity_input)

    # Check results
    mock_get_builder.assert_called_once_with("test_builder")
    mock_graph_builder.assert_called_once()
    mock_agent_class.assert_called_once_with(
        state_graph=mock_state_graph,
        state={"initial": "state"},
    )
    mock_agent_instance.compile_graph.assert_called_once()
    mock_agent_instance.run.assert_called_once_with("test query")

    assert result["result"] == "Agent execution completed successfully"
    assert result["state"] == {"key": "value"}


@pytest.mark.asyncio
async def test_run_agent_no_query(activities):
    """Test running agent with no query."""
    result = await activities.run_agent({"graph_builder_name": "test"})

    assert "error" in result
    assert "No user query" in result["error"]


@pytest.mark.asyncio
async def test_run_agent_no_builder_name(activities):
    """Test running agent with no builder name."""
    result = await activities.run_agent({"user_query": "test"})

    assert "error" in result
    assert "No graph builder name" in result["error"]


@pytest.mark.asyncio
@patch("application_sdk.activities.agents.langgraph.get_graph_builder")
async def test_run_agent_builder_not_found(mock_get_builder, activities):
    """Test running agent when builder not found."""
    mock_get_builder.return_value = None

    result = await activities.run_agent(
        {"user_query": "test", "graph_builder_name": "nonexistent"}
    )

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
@patch("application_sdk.activities.agents.langgraph.get_graph_builder")
async def test_run_agent_build_graph_error(
    mock_get_builder, activities, mock_graph_builder
):
    """Test running agent when builder raises error."""
    mock_get_builder.return_value = mock_graph_builder
    mock_graph_builder.side_effect = Exception("Test build error")

    result = await activities.run_agent(
        {"user_query": "test", "graph_builder_name": "test_builder"}
    )

    assert "error" in result
    assert "Error building graph" in result["error"]


@pytest.mark.asyncio
@patch("application_sdk.activities.agents.langgraph.get_graph_builder")
@patch("application_sdk.activities.agents.langgraph.LangGraphAgent")
async def test_run_agent_runtime_error(
    mock_agent_class, mock_get_builder, activities, mock_graph_builder, mock_state_graph
):
    """Test running agent when agent raises error."""
    # Set up mocks
    mock_get_builder.return_value = mock_graph_builder
    mock_graph_builder.return_value = mock_state_graph
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.side_effect = Exception("Test runtime error")
    mock_agent_class.return_value = mock_agent_instance

    result = await activities.run_agent(
        {"user_query": "test", "graph_builder_name": "test_builder"}
    )

    assert "error" in result
    assert "Error running agent task" in result["error"]
