"""
LangGraph activities interface.

This module provides the activities interface for LangGraph agent operations,
including graph compilation and task execution.
"""

import importlib
from typing import Any, Callable, Dict, Optional, Union

from temporalio import activity

from application_sdk.activities import ActivitiesInterface
from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)

try:
    from langgraph.graph import StateGraph

    from application_sdk.agents.langgraph_agent import LangGraphAgent

    LANGGRAPH_AVAILABLE = True
except ImportError:
    logger.warning("LangGraph is not installed, agent functionality will be disabled")
    LANGGRAPH_AVAILABLE = False
    StateGraph = Any  # type: ignore
    LangGraphAgent = Any  # type: ignore

# Registry to store graph builder functions
_graph_builders: Dict[str, Callable[..., Union[StateGraph, Any]]] = {}


def register_graph_builder(
    name: str, builder_func: Callable[..., Union[StateGraph, Any]]
) -> None:
    """Register a graph builder function.

    Args:
        name: The name to register the builder under
        builder_func: A function that returns a StateGraph
    """
    if not LANGGRAPH_AVAILABLE:
        logger.warning(
            "LangGraph is not installed, graph builder registration will be ignored"
        )
        return

    _graph_builders[name] = builder_func


def get_graph_builder(name: str) -> Optional[Callable[..., Union[StateGraph, Any]]]:
    """Get a graph builder by name.

    Args:
        name: The name of the builder to get

    Returns:
        The builder function or None if not found
    """
    if not LANGGRAPH_AVAILABLE:
        logger.warning("LangGraph is not installed, cannot get graph builder")
        return None

    # Check if builder is directly in registry
    if name in _graph_builders:
        return _graph_builders[name]

    # Try to import from module path (e.g. "my_module.my_builder")
    try:
        if "." in name:
            module_path, func_name = name.rsplit(".", 1)
            module = importlib.import_module(module_path)
            if hasattr(module, func_name):
                builder = getattr(module, func_name)
                # Cache for future use
                _graph_builders[name] = builder
                return builder
    except ImportError:
        logger.error(f"Could not import graph builder module: {name}")
    except Exception as e:
        logger.error(f"Error loading graph builder: {str(e)}")

    return None


class LangGraphActivities(ActivitiesInterface):
    """Activities for LangGraph agent operations.

    This class defines the activities that can be executed as part of
    LangGraph workflows, including graph compilation and task execution.
    """

    @activity.defn
    async def run_agent(self, activity_input: Dict[str, Any]) -> Dict[str, Any]:
        """Runs the LangGraph agent with the given task.

        Args:
            activity_input (Dict[str, Any]): Input for the activity,
                including user query, state, and graph_builder_name.

        Returns:
            Dict[str, Any]: Result of the agent task execution.
        """
        if not LANGGRAPH_AVAILABLE:
            return {
                "error": "LangGraph is not installed. Please install the package with 'pip install langgraph' or use the langgraph_agent extra."
            }

        try:
            user_query = activity_input.get("user_query")
            if not user_query:
                return {"error": "Error: No user query provided."}

            graph_builder_name = activity_input.get("graph_builder_name")
            if not graph_builder_name:
                return {"error": "Error: No graph builder name provided."}

            # Get the graph builder function
            graph_builder = get_graph_builder(graph_builder_name)
            if not graph_builder:
                return {
                    "error": f"Error: Graph builder '{graph_builder_name}' not found."
                }

            # Build the StateGraph
            try:
                state_graph = graph_builder()
            except Exception as e:
                logger.error(f"Error building graph: {str(e)}")
                return {"error": f"Error building graph: {str(e)}"}

            # Initialize the agent
            agent = LangGraphAgent(
                state_graph=state_graph,
                state=activity_input.get("state", {"messages": []}),
            )

            # Compile and run the graph
            agent.compile_graph()
            response = agent.run(user_query)
            state = agent.state

            return {
                "result": "Agent execution completed successfully",
                "response": response,
                "state": state,
            }
        except Exception as e:
            error_msg = f"Error running agent task: {str(e)}"
            logger.error(error_msg)
            return {"error": error_msg}
