import uuid
from typing import Any, Dict, Optional, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from application_sdk.agents.agent import AgentInterface
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter


class LangGraphAgent(AgentInterface):
    """
    Base agent class that can compile a langgraph workflow and run it.
    the workflow is a langgraph StateGraph object.
    Example:
    state = {
        "messages": [],
        "answer": "",
    }
    workflow = StateGraph()
    workflow.add_node("node1", node1)
    workflow.add_edge(START, "node1")
    workflow.add_edge("node1", END)
    agent = LangGraphAgent(workflow=workflow, state=state, config={"configurable": {"thread_id": uuid.uuid4()}})
    agent.compile_graph()
    agent.visualize()
    agent.run(task="What is the capital of France?")
    """

    workflow: Optional[StateGraph]
    graph: Optional[CompiledStateGraph]
    logger: AtlanLoggerAdapter

    def __init__(
        self,
        workflow: Optional[StateGraph] = None,
        state: Optional[Dict[str, Any]] = None,
        config: Optional[RunnableConfig] = None,
        logger: Optional[AtlanLoggerAdapter] = None,
    ):
        """
        Initialize a langgraph agent with a workflow configuration.

        Args:
            workflow: The workflow to execute
            state: The initial state of the workflow
            config (optional): The configuration of the workflow
            logger (optional): Logger instance for the agent
        """
        self.workflow = workflow
        self._state = state
        self._config = config or RunnableConfig(
            configurable={"thread_id": uuid.uuid4()}
        )
        self.logger = logger or AtlanLoggerAdapter(__name__)

    def compile_graph(self) -> CompiledStateGraph:
        """
        Compile the workflow into an executable graph.
        This method should be implemented by specific agent implementations
        to use their preferred graph execution engine.
        """
        if not self.workflow:
            raise ValueError("Workflow not initialized")
        self.graph = self.workflow.compile()
        return self.graph

    def run(self, task: Optional[str] = None) -> None:
        """
        Run the workflow with the given initial state.
        This method should be implemented by specific agent implementations.
        """
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

    def visualize(self) -> None:
        try:
            if not self.graph:
                raise ValueError("Graph not compiled")
            png_data = self.graph.get_graph().draw_mermaid_png()

            output_path = "graph.png"
            with open(output_path, "wb") as f:
                f.write(png_data)
            self.logger.info(f"Graph visualization saved to {output_path}")
        except Exception as e:
            self.logger.error(f"Failed to save graph visualization: {str(e)}")
