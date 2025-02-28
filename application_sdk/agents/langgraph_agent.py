import uuid
from typing import Any, Dict, Optional, Union

from application_sdk.agents.agent import AgentInterface
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter, get_logger

logger = get_logger(__name__)

try:
    from langchain_core.messages import HumanMessage
    from langchain_core.runnables.config import RunnableConfig
    from langgraph.graph import StateGraph
    from langgraph.graph.state import CompiledStateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:
    logger.warning(
        "LangGraph dependencies not installed, agent functionality will be limited"
    )
    LANGGRAPH_AVAILABLE = False
    # Define dummy types when LangGraph is not available
    HumanMessage = Dict[str, Any]
    RunnableConfig = Dict[str, Any]
    StateGraph = Any
    CompiledStateGraph = Any


class LangGraphAgent(AgentInterface):
    """
    Base agent class that can compile a langgraph workflow and run it.
    the workflow is a langgraph StateGraph object.
    Example:
    state = {
        "messages": [],
        "answer": "",
    }
    state_graph = StateGraph()
    state_graph.add_node("node1", node1)
    state_graph.add_edge(START, "node1")
    state_graph.add_edge("node1", END)
    agent = LangGraphAgent(state_graph=state_graph, state=state, config={"configurable": {"thread_id": uuid.uuid4()}})
    agent.compile_graph()
    png_data = agent.visualize()
    agent.run(task="What is the capital of France?")
    """

    state_graph: Optional[StateGraph]
    graph: Optional[CompiledStateGraph]
    logger: AtlanLoggerAdapter

    def __init__(
        self,
        state_graph: Optional[StateGraph],
        state: Optional[Dict[str, Any]] = None,
        config: Optional[Union[RunnableConfig, Dict[str, Any]]] = None,
        logger: Optional[AtlanLoggerAdapter] = None,
    ):
        """
        Initialize a langgraph agent with a workflow configuration.

        Args:
            state_graph: The langgraphs' StateGraph to compile
            state: The initial state of the workflow
            config (optional): The configuration of the workflow
            logger (optional): Logger instance for the agent
        """
        if not LANGGRAPH_AVAILABLE:
            raise ImportError(
                "LangGraph dependencies are not installed. Please install the package with 'pip install langgraph' or use the langgraph_agent extra."
            )

        self.state_graph = state_graph
        self._state = state
        self._config = config or RunnableConfig(
            configurable={"thread_id": uuid.uuid4()}
        )
        self.logger = logger or AtlanLoggerAdapter(__name__)

    def compile_graph(self) -> Optional[CompiledStateGraph]:
        """
        Compile the langgraph StateGraph into an executable graph.
        """
        if not LANGGRAPH_AVAILABLE:
            self.logger.warning("LangGraph is not installed, cannot compile graph")
            return None

        if not self.state_graph:
            raise ValueError("StateGraph not initialized")
        self.graph = self.state_graph.compile()
        return self.graph

    def run(self, task: Optional[str]) -> None:
        """
        Run the langgraph StateGraph with the given initial state, and task.
        """
        if not LANGGRAPH_AVAILABLE:
            self.logger.warning("LangGraph is not installed, cannot run agent")
            return None

        if self._state is None:
            self._state = {"messages": []}

        if not self.graph:
            self.graph = self.compile_graph()

        try:
            if task:
                self._state["messages"].append(HumanMessage(content=task))
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
        return self._state

    def visualize(self) -> Optional[bytes]:
        """
        Visualize the graph and return the raw bytes of the PNG visualization.

        Returns:
            bytes: The raw bytes of the graph visualization in PNG format.
        """
        if not LANGGRAPH_AVAILABLE:
            self.logger.warning("LangGraph is not installed, cannot visualize graph")
            return None

        try:
            if not self.graph:
                raise ValueError("Graph not compiled")
            png_data = self.graph.get_graph().draw_mermaid_png()
            return png_data
        except Exception as e:
            self.logger.error("Error visualizing graph: %s", str(e))
            raise
