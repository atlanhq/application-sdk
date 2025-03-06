"""
LangGraph workflow interface.

This module provides the workflow implementation for LangGraph agent operations,
including graph compilation and task execution.
"""

from datetime import timedelta
from typing import Any, Callable, Dict, List, Sequence, Type, Union, cast

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.agents.langgraph import LangGraphActivities
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


@workflow.defn
class LangGraphWorkflow(WorkflowInterface):
    """Temporal Workflow to execute LangGraph agent steps.

    This workflow handles the execution of LangGraph agent operations,
    including graph compilation and task execution.

    Attributes:
        activities_cls (Type[ActivitiesInterface]): The activities class for
            LangGraph operations.
        state (Dict[str, Union[str, List[Any]]]): The current state of the workflow.
    """

    activities_cls: Type[ActivitiesInterface] = cast(
        Type[ActivitiesInterface], LangGraphActivities
    )

    def __init__(self):
        self.state: Dict[str, Union[str, List[Any]]] = {
            "messages": [],
            "enriched_query": "",
            "steps_list": [],
            "validation_response": "",
        }

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities for this workflow.

        Args:
            activities (ActivitiesInterface): The activities interface instance.

        Returns:
            Sequence[Callable[..., Any]]: List of activity methods to be executed.
        """
        activities = cast(LangGraphActivities, activities)
        return [
            activities.run_agent,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        """Execute the LangGraph workflow.

        This method:
        1. Initializes the workflow state
        2. Compiles the LangGraph
        3. Executes the user's task

        Args:
            workflow_config (Dict[str, Any]): Includes workflow_id and other parameters
                workflow_id is used to extract the workflow configuration from the
                state store.
        """
        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStoreInput.extract_configuration(
            workflow_id
        )

        workflow_args["state"] = self.state

        retry_policy = RetryPolicy(
            maximum_attempts=3,
            backoff_coefficient=2,
        )

        user_query = workflow_args.get("user_query")
        if not user_query:
            logger.warning("No user query provided")
            return

        activity_input: Dict[str, Any] = {
            "user_query": user_query,
            "state": self.state,
            "state_graph": workflow_args.get("state_graph"),
        }

        activities = cast(Type[LangGraphActivities], self.activities_cls)
        await workflow.execute_activity_method(
            activities.run_agent,
            args=[activity_input],
            retry_policy=retry_policy,
            schedule_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
