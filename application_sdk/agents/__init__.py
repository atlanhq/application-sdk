from datetime import timedelta
from typing import Annotated, Any, Callable, Dict, Sequence

from langgraph.graph.message import AnyMessage, add_messages
from temporalio import workflow
from temporalio.common import RetryPolicy
from typing_extensions import TypedDict

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


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

        activity_input: Dict[str, Any] = {
            "user_query": user_query,
            "state": self.state,
            "graph_builder_name": graph_builder_name,
        }

        # Get the activities instance
        activities_instance = self.activities_cls()

        # Execute the activity
        result = await workflow.execute_activity_method(
            activities_instance.run_agent,
            args=[activity_input],
            retry_policy=retry_policy,
            schedule_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
        return result
