from datetime import timedelta
from typing import Annotated, Any, Callable, Dict, Sequence, Type

from langgraph.graph.message import AnyMessage, add_messages
from temporalio import workflow
from temporalio.common import RetryPolicy
from typing_extensions import TypedDict

from application_sdk.activities.agents.langgraph import LangGraphActivities
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
        activities_cls (Type[LangGraphActivities]): The activities class for
            LangGraph operations.
        state (Dict[str, Union[str, List[Any]]]): The current state of the workflow.
    """

    activities_cls: Type[LangGraphActivities] = LangGraphActivities

    def __init__(self):
        self.state = AgentState(messages=[])

    @staticmethod
    def get_activities(activities: LangGraphActivities) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities for this workflow.

        Args:
            activities (LangGraphActivities): The activities interface instance.

        Returns:
            Sequence[Callable[..., Any]]: List of activity methods to be executed.
        """
        return [
            activities.run_agent,
        ]

    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the LangGraph workflow.

        This method:
        1. Initializes the workflow state
        2. Executes the LangGraph agent using the configured builder

        Args:
            workflow_args (Dict[str, Any]): Arguments for the workflow,
                including user query, state, and graph_builder_name.

        Returns:
            Dict[str, Any]: Result of the workflow execution.
        """
        # Initialize or update the state
        if "state" in workflow_args:
            self.state = workflow_args["state"]
        else:
            workflow_args["state"] = self.state

        retry_policy = RetryPolicy(
            maximum_attempts=3,
            backoff_coefficient=2,
        )

        user_query = workflow_args.get("user_query")
        if not user_query:
            return {"error": "No user query provided"}

        graph_builder_name = workflow_args.get("graph_builder_name")
        if not graph_builder_name:
            return {"error": "No graph builder name provided"}

        activity_input: Dict[str, Any] = {
            "user_query": user_query,
            "state": self.state,
            "graph_builder_name": graph_builder_name,
        }

        result = await workflow.execute_activity_method(
            self.activities_cls.run_agent,
            args=[activity_input],
            retry_policy=retry_policy,
            schedule_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        return result
