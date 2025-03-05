from datetime import timedelta
from typing import Any, Callable, Dict, List, Sequence

from temporalio import workflow
from temporalio.common import RetryPolicy
from typing_extensions import TypedDict

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)

try:
    from langchain_openai import AzureChatOpenAI
    from langgraph.graph.message import AnyMessage, add_messages

    LANGGRAPH_AVAILABLE = True
except ImportError:
    logger.warning("LangGraph is not installed, agent functionality will be limited")
    LANGGRAPH_AVAILABLE = False
    # Define dummy types when LangGraph is not available
    AnyMessage = Dict[str, Any]

    def add_messages(x: List[AnyMessage]) -> List[AnyMessage]:
        return x


class AgentState(TypedDict):
    """State for agent operations, works with or without LangGraph."""

    messages: (
        "List[AnyMessage]"  # Type will be different based on LangGraph availability
    )


@workflow.defn
class LangGraphWorkflow(WorkflowInterface):
    """Temporal Workflow to execute LangGraph agent steps.

    This workflow handles the execution of LangGraph agent operations,
    including graph compilation and task execution.

    Attributes:
        activities_cls: The activities class for LangGraph operations.
        state: The current state of the workflow.
    """

    def __init__(self):
        self.state = AgentState(messages=[])

    @classmethod
    def activities_cls(cls):
        """Returns an instance of the activities class."""
        try:
            cls._validate_langgraph_installed()
        except ValueError as e:
            logger.warning(str(e))
            return None

        # Import inside the method to avoid circular import
        from application_sdk.activities.agents.langgraph import LangGraphActivities

        return LangGraphActivities()

    @staticmethod
    def get_activities(activities: Any) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities for this workflow.

        Args:
            activities: The activities interface instance.

        Returns:
            Sequence[Callable[..., Any]]: List of activity methods to be executed.
        """
        if not activities:
            return []

        return [
            activities.run_agent,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the LangGraph workflow.

        This method:
        1. Initializes the workflow state
        2. Executes the LangGraph agent using the configured builder

        Args:
            workflow_config: Arguments for the workflow,
                including user query, state, and graph_builder_name.

        Returns:
            Dict[str, Any]: The result of the LangGraph agent execution.
        """
        try:
            self._validate_langgraph_installed()
        except ValueError as e:
            workflow.logger.error(str(e))
            return {"error": str(e)}

        # Initialize or update the state
        if "state" in workflow_config:
            self.state = workflow_config["state"]
        else:
            workflow_config["state"] = self.state

        retry_policy = RetryPolicy(
            maximum_attempts=3,
            backoff_coefficient=2,
        )

        user_query = workflow_config.get("user_query")
        if not user_query:
            workflow.logger.error("No user query provided")
            return {"error": "No user query provided"}

        graph_builder_name = workflow_config.get("graph_builder_name")
        if not graph_builder_name:
            workflow.logger.error("No graph builder name provided")
            return {"error": "No graph builder name provided"}

        # Get the activities instance
        activities_instance = self.activities_cls()
        if not activities_instance:
            return {
                "error": "LangGraph activities are not available. Please install the package with 'pip install application-sdk[langgraph_agent]' or use the langgraph_agent extra."
            }

        activity_input: Dict[str, Any] = {
            "user_query": user_query,
            "state": self.state,
            "graph_builder_name": graph_builder_name,
        }

        # Get timeout configurations with defaults
        schedule_to_close_timeout = workflow_config.get("schedule_to_close_timeout")
        heartbeat_timeout = workflow_config.get("heartbeat_timeout")

        # Use timedelta with default values if None
        schedule_to_close = timedelta(
            seconds=300
            if schedule_to_close_timeout is None
            else schedule_to_close_timeout
        )
        heartbeat = timedelta(
            seconds=30 if heartbeat_timeout is None else heartbeat_timeout
        )

        # Execute the activity
        result = await workflow.execute_activity_method(
            activities_instance.run_agent,
            args=[activity_input],
            retry_policy=retry_policy,
            schedule_to_close_timeout=schedule_to_close,
            heartbeat_timeout=heartbeat,
        )
        return result

    @staticmethod
    def _validate_langgraph_installed() -> None:
        """
        Validate that LangGraph is installed.

        Raises:
            ValueError: If LangGraph is not installed.
        """
        if not LANGGRAPH_AVAILABLE:
            raise ValueError(
                "LangGraph is not installed. Please install it with `pip install application-sdk[langgraph_agent]` or use the langgraph_agent extra."
            )


class LLM:
    """LLM class to get an LLM instance."""

    @staticmethod
    def get_llm(
        api_key: str,
        api_version: str,
        azure_endpoint: str,
        azure_deployment: str,
        temperature: float = 0.0,
    ) -> "AzureChatOpenAI":
        """
        Get an LLM instance
        Future: Add support for other LLM Clients as well.
        """
        if not LANGGRAPH_AVAILABLE:
            raise ImportError(
                "LangGraph is not installed, agent functionality will be limited"
            )

        llm = AzureChatOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=azure_endpoint,
            azure_deployment=azure_deployment,
            temperature=temperature,
        )
        return llm
