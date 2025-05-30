"""Activities module for workflow task implementations.

This module provides base classes and interfaces for implementing workflow activities.
It includes state management functionality and defines the basic structure for
activity implementations.

Example:
    >>> from application_sdk.activities import ActivitiesInterface
    >>>
    >>> class MyActivities(ActivitiesInterface[MyHandler]):
    ...     async def my_activity(self, workflow_args: Dict[str, Any]) -> None:
    ...         state = await self._get_state(workflow_args)
    ...         await state.handler.do_something()
"""

from abc import ABC
from typing import Any, Dict, Generic, Optional, TypeVar

from pydantic import BaseModel
from temporalio import activity

from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.common.error_codes import OrchestratorError
from application_sdk.handlers import HandlerInterface
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger

# Define a custom type for the handler
HandlerType = TypeVar("HandlerType", bound=HandlerInterface)


class ActivitiesState(BaseModel, Generic[HandlerType]):
    """Base state model for workflow activities.

    This class provides the base state structure for workflow activities,
    including handler configuration and workflow arguments.

    Attributes:
        handler: Handler instance for activity-specific operations.
            Must be a subclass of HandlerInterface.
        workflow_args: Arguments passed to the workflow.
            Contains configuration and runtime parameters.

    Example:
        >>> state = ActivitiesState[MyHandler](
        ...     handler=MyHandler(),
        ...     workflow_args={"param": "value"}
        ... )
    """

    model_config = {"arbitrary_types_allowed": True}
    handler: Optional[HandlerType] = None
    workflow_args: Optional[Dict[str, Any]] = None


ActivitiesStateType = TypeVar("ActivitiesStateType", bound=ActivitiesState)


class ActivitiesInterface(ABC, Generic[ActivitiesStateType]):
    """Abstract base class defining the interface for workflow activities.

    This class provides state management functionality and defines the basic structure
    for activity implementations. Activities can access shared state and handler
    instances through this interface.

    Attributes:
        _state: Dictionary mapping workflow IDs to their respective states.

    Example:
        >>> class MyActivities(ActivitiesInterface[MyHandler]):
        ...     async def setup(self, handler: MyHandler) -> None:
        ...         state = await self._get_state({})
        ...         state.handler = handler
    """

    def __init__(self):
        """Initialize the activities interface with an empty state dictionary.

        The state dictionary maps workflow IDs to their respective ActivitiesState
        instances, allowing for isolation between different workflow executions.
        """
        self._state: Dict[str, ActivitiesStateType] = {}

    # State methods
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Initialize or update the state for the current workflow.

        This method sets up the initial state for a workflow or updates an existing
        state with new workflow arguments. The state is stored in a dictionary
        keyed by workflow ID.

        Args:
            workflow_args: Arguments for the workflow, containing
                configuration and runtime parameters.

        Example:
            >>> await activity._set_state({
            ...     "workflow_id": "123",
            ...     "metadata": {"key": "value"}
            ... })

        Note:
            The workflow ID is automatically retrieved from the current activity context.
            If no state exists for the current workflow, a new one will be created.
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = ActivitiesState()

        self._state[workflow_id].workflow_args = workflow_args

    async def _get_state(self, workflow_args: Dict[str, Any]) -> ActivitiesStateType:
        """Retrieve the state for the current workflow.

        If state doesn't exist, it will be initialized using _set_state.

        Args:
            workflow_args: Dictionary containing workflow arguments and configuration.

        Returns:
            The state data for the current workflow.

        Raises:
            Exception: If there is an error retrieving or initializing the state.
                The state will be cleaned up in case of an error.

        Note:
            This method will automatically initialize state if it doesn't exist.
        """
        try:
            workflow_id = get_workflow_id()
            if workflow_id not in self._state:
                await self._set_state(workflow_args)
            return self._state[workflow_id]
        except OrchestratorError as e:
            logger.error(
                f"Error getting state: {str(e)}",
                error_code=OrchestratorError.ORCHESTRATOR_CLIENT_ACTIVITY_ERROR.code,
                exc_info=e,
            )
            await self._clean_state()
            raise

    async def _clean_state(self):
        """Remove the state data for the current workflow.

        This method is typically called when cleaning up after workflow completion
        or when handling errors that require state reset.

        Note:
            Failures during cleanup are logged but do not raise exceptions to avoid
            masking the original error that triggered the cleanup.
        """
        try:
            workflow_id = get_workflow_id()
            if workflow_id in self._state:
                self._state.pop(workflow_id)
        except OrchestratorError as e:
            logger.warning("Failed to clean state", exc_info=e)

    @activity.defn
    @auto_heartbeater
    async def get_workflow_args(
        self, workflow_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Activity to safely retrieve workflow configuration from state store.

        Args:
            workflow_config: Dictionary containing workflow_id and other parameters

        Returns:
            Dict containing the complete workflow configuration

        Raises:
            IOError: If configuration cannot be retrieved from state store
        """
        workflow_id = workflow_config.get("workflow_id")
        if not workflow_id:
            raise ValueError("workflow_id is required in workflow_config")

        try:
            # This already handles the Dapr call internally
            return StateStoreInput.extract_configuration(workflow_id)
        except Exception as e:
            logger.error(
                f"Failed to retrieve workflow configuration for {workflow_id}: {str(e)}",
                exc_info=e,
            )
            raise

    @activity.defn
    @auto_heartbeater
    async def preflight_check(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Perform preflight checks before workflow execution.

        This method validates the workflow configuration and ensures all required
        resources are available before starting the main workflow execution.

        Args:
            workflow_args: Dictionary containing workflow arguments and configuration.
                Must include a 'metadata' key with workflow-specific settings.

        Returns:
            Dictionary containing the results of the preflight check.

        Raises:
            ValueError: If the handler is not found or if the preflight check fails.
            Exception: For any other errors during the preflight check.

        Note:
            This method is decorated with @activity.defn to mark it as a Temporal activity
            and with @auto_heartbeater to automatically send heartbeats during execution.
        """
        logger.info("Starting preflight check")

        try:
            state: ActivitiesStateType = await self._get_state(workflow_args)
            handler = state.handler

            if not handler:
                raise ValueError("Preflight check handler not found")

            result = await handler.preflight_check(
                {"metadata": workflow_args["metadata"]}
            )

            if not result or "error" in result:
                raise ValueError("Preflight check failed")

            logger.info("Preflight check completed successfully")
            return result

        except OrchestratorError as e:
            logger.error(
                f"Preflight check failed: {str(e)}",
                error_code=OrchestratorError.ORCHESTRATOR_CLIENT_ACTIVITY_ERROR.code,
                exc_info=e,
            )
            raise
