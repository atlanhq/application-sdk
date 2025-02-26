import uuid
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import StateGraph

from application_sdk.agents.langgraph_agent import LangGraphAgent
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter


@pytest.fixture
def mock_state_graph():
    """Create a mock StateGraph for testing."""
    graph = MagicMock(spec=StateGraph)
    return graph


@pytest.fixture
def mock_compiled_graph():
    """Create a mock CompiledStateGraph for testing."""
    graph = MagicMock()

    # Mock the stream method to return chunks
    # Create a mock that doesn't override the messages by default
    graph.stream.return_value = [{}]

    # Mock the get_graph method for visualization
    graph_viz = MagicMock()
    graph_viz.draw_mermaid_png.return_value = b"test_png_data"
    graph.get_graph.return_value = graph_viz

    return graph


@pytest.fixture
def test_state():
    """Create a test state for the agent."""
    return {"messages": []}


@pytest.fixture
def test_config():
    """Create a test config for the agent."""
    return RunnableConfig(configurable={"thread_id": uuid.uuid4()})


@pytest.fixture
def agent(mock_state_graph, test_state, test_config):
    """Create a LangGraphAgent for testing."""
    return LangGraphAgent(
        state_graph=mock_state_graph,
        state=test_state,
        config=test_config,
        logger=MagicMock(spec=AtlanLoggerAdapter),
    )


def test_init(agent, mock_state_graph, test_state, test_config):
    """Test the initialization of LangGraphAgent."""
    assert agent.state_graph == mock_state_graph
    assert agent._state == test_state
    assert agent._config == test_config
    assert isinstance(agent.logger, MagicMock)


def test_init_with_defaults():
    """Test initialization with default values."""
    agent = LangGraphAgent(state_graph=MagicMock(spec=StateGraph))

    # The state is None by default in the implementation, only initialized in run
    assert agent._state is None
    # Check if the _config has the expected structure instead of using isinstance
    assert "configurable" in agent._config
    assert "thread_id" in agent._config["configurable"]
    assert isinstance(agent.logger, AtlanLoggerAdapter)


def test_compile_graph(agent, mock_state_graph, mock_compiled_graph):
    """Test compiling the graph."""
    mock_state_graph.compile.return_value = mock_compiled_graph

    result = agent.compile_graph()

    mock_state_graph.compile.assert_called_once()
    assert result == mock_compiled_graph
    assert agent.graph == mock_compiled_graph


def test_compile_graph_no_state_graph(agent):
    """Test compiling with no state graph."""
    agent.state_graph = None

    with pytest.raises(ValueError, match="StateGraph not initialized"):
        agent.compile_graph()


def test_run_with_task(agent, mock_compiled_graph):
    """Test running the agent with a task."""
    agent.graph = mock_compiled_graph

    # Set up the mock to return a state with the correct message
    mock_state = {"messages": [HumanMessage(content="Test query")]}
    mock_compiled_graph.stream.return_value = [mock_state]

    agent.run(task="Test query")

    # Check that the message was added
    assert len(agent._state["messages"]) > 0
    assert isinstance(agent._state["messages"][-1], HumanMessage)
    assert agent._state["messages"][-1].content == "Test query"

    # Check that the graph was streamed
    mock_compiled_graph.stream.assert_called_once()


def test_run_without_task(agent, mock_compiled_graph):
    """Test running the agent without a task."""
    agent.graph = mock_compiled_graph

    # Set up the mock to return an empty messages state
    mock_state = {"messages": []}
    mock_compiled_graph.stream.return_value = [mock_state]

    agent.run(task=None)

    # Check that no message was added
    assert len(agent._state["messages"]) == 0

    # Check that the graph was streamed
    mock_compiled_graph.stream.assert_called_once()


def test_run_compile_if_needed(agent, mock_state_graph, mock_compiled_graph):
    """Test that run compiles the graph if needed."""
    mock_state_graph.compile.return_value = mock_compiled_graph
    agent.graph = None

    agent.run(task="Test query")

    mock_state_graph.compile.assert_called_once()
    mock_compiled_graph.stream.assert_called_once()


def test_run_error_handling(agent, mock_compiled_graph):
    """Test error handling during run."""
    agent.graph = mock_compiled_graph
    mock_compiled_graph.stream.side_effect = Exception("Test error")

    # Should not raise an exception
    agent.run(task="Test query")

    # Should log the error
    agent.logger.error.assert_called_once()


def test_state_property(agent, test_state):
    """Test the state property."""
    assert agent.state == test_state


def test_visualize(agent, mock_compiled_graph):
    """Test visualizing the graph."""
    agent.graph = mock_compiled_graph

    result = agent.visualize()

    mock_compiled_graph.get_graph.assert_called_once()
    mock_compiled_graph.get_graph().draw_mermaid_png.assert_called_once()
    assert result == b"test_png_data"


def test_visualize_no_graph(agent):
    """Test visualizing with no graph."""
    agent.graph = None

    with pytest.raises(ValueError, match="Graph not compiled"):
        agent.visualize()


def test_visualize_error_handling(agent, mock_compiled_graph):
    """Test error handling during visualization."""
    agent.graph = mock_compiled_graph
    mock_compiled_graph.get_graph().draw_mermaid_png.side_effect = Exception(
        "Test error"
    )

    with pytest.raises(Exception):
        agent.visualize()

    # Should log the error
    agent.logger.error.assert_called_once()
