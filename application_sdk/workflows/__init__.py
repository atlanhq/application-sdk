# ruff: noqa: E402
"""Workflow interface module for Temporal workflows.

This module provides the base workflow interface and common functionality for
all workflow implementations in the application SDK.
"""

import warnings

warnings.warn(
    "application_sdk.workflows is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.app.App with @task decorators instead (see ADR-0005).",
    DeprecationWarning,
    stacklevel=2,
)

from abc import ABC
from datetime import timedelta
from typing import Any, Callable, Dict, Generic, Optional, Sequence, Type, TypeVar

from temporalio import workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.constants import HEARTBEAT_TIMEOUT, START_TO_CLOSE_TIMEOUT
from application_sdk.execution.retry import RetryPolicy, _to_temporal_retry_policy
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

ActivitiesInterfaceType = TypeVar("ActivitiesInterfaceType", bound=ActivitiesInterface)


@workflow.defn
class WorkflowInterface(ABC, Generic[ActivitiesInterfaceType]):
    """Abstract base class for all workflow implementations.

    This class defines the interface that all workflows must implement and provides
    common functionality for workflow execution.

    Attributes:
        activities_cls (Type[ActivitiesInterface]): The activities class to be used
            by the workflow.
        default_heartbeat_timeout (timedelta): The default heartbeat timeout for the
            workflow.
    """

    activities_cls: Type[ActivitiesInterfaceType]

    default_heartbeat_timeout: timedelta = HEARTBEAT_TIMEOUT
    default_start_to_close_timeout: timedelta = START_TO_CLOSE_TIMEOUT

    @staticmethod
    def get_activities(
        activities: ActivitiesInterfaceType,
    ) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities for this workflow.

        This method must be implemented by subclasses to define the activities
        that will be executed as part of the workflow.

        Args:
            activities (ActivitiesInterface): The activities interface instance.

        Returns:
            Sequence[Callable[..., Any]]: List of activity methods to be executed.

        Raises:
            NotImplementedError: If the method is not implemented by a subclass.
        """
        raise NotImplementedError("Workflow get_activities method not implemented")

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
        # Get the workflow configuration from the state store
        workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
            self.activities_cls.get_workflow_args,
            workflow_config,  # Pass the whole config containing workflow_id
            retry_policy=_to_temporal_retry_policy(
                RetryPolicy(max_attempts=3, backoff_coefficient=2)
            ),
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        logger.info("Starting workflow execution")

        retry_policy = _to_temporal_retry_policy(
            RetryPolicy(max_attempts=2, backoff_coefficient=2)
        )

        await workflow.execute_activity_method(
            self.activities_cls.preflight_check,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
