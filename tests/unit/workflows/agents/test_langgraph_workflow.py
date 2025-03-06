from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from langgraph.graph import StateGraph

from application_sdk.agents.langgraph_agent import LangGraphAgent


@pytest.fixture
def mock_state_graph() -> StateGraph:
    """Create a mock StateGraph."""
    return MagicMock(spec=StateGraph)


@pytest.fixture
def agent_with_mock_graph(mock_state_graph: StateGraph) -> LangGraphAgent:
    """Create a LangGraphAgent instance with mock state graph."""
    return LangGraphAgent(state_graph=mock_state_graph)


def test_init() -> None:
    """Test initialization of LangGraphAgent."""
    agent = LangGraphAgent(state_graph=MagicMock(spec=StateGraph))

    # Check that the state is initialized correctly to default value
    assert agent.state == {"messages": []}


def test_init_with_state() -> None:
    """Test initialization with state."""
    initial_state: Dict[str, List[Any]] = {"messages": []}
    agent = LangGraphAgent(state_graph=MagicMock(spec=StateGraph), state=initial_state)
    assert agent.state == initial_state


@pytest.mark.asyncio
async def test_run_without_task(agent_with_mock_graph: LangGraphAgent) -> None:
    """Test running the agent without a task."""
    # Run without task
    result = agent_with_mock_graph.run(None)
    assert result is None


@pytest.mark.asyncio
async def test_run_without_graph() -> None:
    """Test running without a state graph."""
    # Create agent without state graph
    agent = LangGraphAgent(state_graph=None)

    with pytest.raises(ValueError, match="StateGraph not initialized"):
        agent.run("test task")


@pytest.mark.asyncio
async def test_run_with_state() -> None:
    """Test running the agent with initial state."""
    # Create initial state
    initial_state: Dict[str, List[Any]] = {"messages": []}

    # Create mock state graph that returns chunks
    mock_graph = MagicMock()
    mock_graph.stream.return_value = [{"messages": []}]

    # Create agent
    agent = LangGraphAgent(state_graph=MagicMock(spec=StateGraph), state=initial_state)
    agent.graph = mock_graph

    # Run agent
    agent.run("test task")

    # Check that the state was updated
    assert agent.state is not None
    assert "messages" in agent.state
