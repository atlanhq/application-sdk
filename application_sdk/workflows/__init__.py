"""Workflow interface module for Temporal workflows.

This module provides the base workflow interface and common functionality for
all workflow implementations in the application SDK.
"""

from abc import ABC
from datetime import timedelta
from typing import Any, Callable, Dict, Generic, Sequence, Type, TypeVar

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput

logger = get_logger(__name__)

T = TypeVar("T", bound=ActivitiesInterface)


@workflow.defn
class WorkflowInterface(ABC, Generic[T]):
    """Abstract base class for all workflow implementations.

    This class defines the interface that all workflows must implement and provides
    common functionality for workflow execution.

    Attributes:
        activities_cls (Type[T]): The activities class to be used
            by the workflow.
        default_heartbeat_timeout (timedelta): The default heartbeat timeout for the
            workflow.
    """

    activities_cls: Type[T]

    default_heartbeat_timeout: timedelta | None = timedelta(seconds=10)
    default_start_to_close_timeout: timedelta | None = timedelta(hours=2)

    @staticmethod
    def get_activities(activities: T) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities for this workflow.

        This method must be implemented by subclasses to define the activities
        that will be executed as part of the workflow.

        Args:
            activities (T): The activities interface instance.

        Returns:
            Sequence[Callable[..., Any]]: List of activity methods to be executed.

        Raises:
            NotImplementedError: If the method is not implemented by a subclass.
        """
        raise NotImplementedError("Workflow get_activities method not implemented")

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        """Run the workflow with the given configuration.

        This method provides the base implementation for workflow execution. It:
        1. Extracts workflow configuration from the state store
        2. Sets up workflow run ID and retry policy
        3. Executes the preflight check activity

        Args:
            workflow_config (Dict[str, Any]): Includes workflow_id and other parameters
                workflow_id is used to extract the workflow configuration from the
                state store.
        """
        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStoreInput.extract_configuration(
            workflow_id
        )

        logger.info(
            "Starting workflow execution",
        )

        try:
            workflow_run_id = workflow.info().run_id
            workflow_args["workflow_run_id"] = workflow_run_id

            retry_policy = RetryPolicy(maximum_attempts=2, backoff_coefficient=2)

            result = await workflow.execute_activity_method(
                self.activities_cls.preflight_check,
                args=[workflow_args],
                retry_policy=retry_policy,
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )

            logger.info("Workflow completed successfully")
            return result

        except Exception as e:
            logger.error(f"Workflow execution failed: {str(e)}", exc_info=True)
            raise
