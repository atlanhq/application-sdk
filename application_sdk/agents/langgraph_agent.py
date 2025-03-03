from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from application_sdk.agents.agent import AgentInterface
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter, get_logger

logger = get_logger(__name__)

try:
    from langchain_core.messages import HumanMessage
    from langgraph.graph import StateGraph

    has_langgraph = True
except ImportError:
    logger.error(
        "LangGraph dependencies not installed. Please install the package with "
        "'pip install application-sdk[langgraph]' or 'pip install langgraph'."
    )
    has_langgraph = False
    # Define dummy types when LangGraph is not available
    HumanMessage = Dict[str, Any]  # type: ignore
    StateGraph = Any  # type: ignore


class LangGraphAgent(AgentInterface):
    """
    Base agent class that can compile a langgraph workflow and run it.
    The workflow is a langgraph StateGraph object.

    Example:
    ```python
    state = {
        "messages": [],
        "answer": "",
    }
    state_graph = StateGraph()
    state_graph.add_node("node1", node1)
    state_graph.add_edge(START, "node1")
    state_graph.add_edge("node1", END)
    agent = LangGraphAgent(state_graph=state_graph, state=state)
    agent.compile_graph()
    agent.run("What is the capital of France?")
    ```
    """

    def __init__(
        self,
        state_graph: Any,
        state: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        logger: Optional[AtlanLoggerAdapter] = None,
    ):
        """
        Initialize a langgraph agent with a workflow configuration.

        Args:
            state_graph: The langgraphs' StateGraph to compile
            state: The initial state of the workflow
            config: The configuration of the workflow
            logger: Logger instance for the agent
        """
        self._validate_langgraph_installed()

        self.state_graph = state_graph
        self._state: Dict[str, Any] = state if state is not None else {"messages": []}
        self._config = config or {"configurable": {"thread_id": uuid.uuid4()}}
        self.logger = logger or AtlanLoggerAdapter(__name__)
        self.graph = None

    @staticmethod
    def _validate_langgraph_installed() -> None:
        """
        Validate that LangGraph is installed.

        Raises:
            ValueError: If LangGraph is not installed.
        """
        if not has_langgraph:
            raise ValueError(
                "LangGraph is not installed. Please install it with `pip install langgraph`."
            )

        # Use StateGraph to confirm it's available
        _ = StateGraph  # Reference StateGraph to satisfy linter

    def compile_graph(self) -> Any:
        """
        Compile the langgraph StateGraph into an executable graph.

        Returns:
            The compiled graph

        Raises:
            ValueError: If state_graph is None
        """
        self._validate_langgraph_installed()

        if not self.state_graph:
            raise ValueError("StateGraph not initialized")

        self.graph = self.state_graph.compile()
        return self.graph

    def run(self, task: Optional[str] = None) -> None:
        """
        Run the langgraph StateGraph with the given initial state and task.

        Args:
            task: Optional task/query to process

        Raises:
            ValueError: If graph is not compiled
        """
        self._validate_langgraph_installed()

        if not self.graph:
            self.graph = self.compile_graph()

        if task and "messages" in self._state:
            self._state["messages"].append(HumanMessage(content=task))  # type: ignore

        try:
            for chunk in self.graph.stream(
                self._state, stream_mode="values", config=self._config
            ):
                self._state = chunk
                if chunk.get("messages"):
                    chunk["messages"][-1].pretty_print()
        except Exception as e:
            self.logger.error(f"Error running workflow: {str(e)}")

    @property
    def state(self) -> Optional[Dict[str, Any]]:
        return self._state  # type: ignore

    def visualize(self) -> Optional[bytes]:
        """
        Visualize the graph and return the raw bytes of the PNG visualization.

        Returns:
            bytes: The raw bytes of the graph visualization in PNG format.

        Raises:
            ValueError: If graph is not compiled
        """
        self._validate_langgraph_installed()

        if not self.graph:
            self.graph = self.compile_graph()

        try:
            png_data: bytes = self.graph.get_graph().draw_mermaid_png()
            return png_data
        except Exception as e:
            self.logger.error(f"Error visualizing graph: {str(e)}")
            return None
