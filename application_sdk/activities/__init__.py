from abc import ABC
from typing import Any, Dict, Optional

from pydantic import BaseModel
from temporalio import activity

from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.handlers import HandlerInterface

logger = get_logger()


class ActivitiesState(BaseModel):
    """Base state model for workflow activities.

    This class provides the base state structure for workflow activities,
    including handler configuration and workflow arguments.

    Attributes:
        handler (Optional[HandlerInterface]): Handler instance for activity-specific
            operations. Defaults to None.
        workflow_args (Optional[Dict[str, Any]]): Arguments passed to the workflow.
            Defaults to None.
    """

    model_config = {"arbitrary_types_allowed": True}
    handler: Optional[HandlerInterface] = None
    workflow_args: Optional[Dict[str, Any]] = None


class ActivitiesInterface(ABC):
    """Abstract base class defining the interface for workflow activities.

    This class provides state management functionality and defines the basic structure
    for activity implementations.
    """

    def __init__(self):
        """Initialize the activities interface with an empty state dictionary."""
        self._state: Dict[str, ActivitiesState] = {}

    # State methods
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Initialize or update the state for the current workflow.

        This method sets up the initial state for a workflow or updates an existing
        state with new workflow arguments. The state is stored in a dictionary
        keyed by workflow ID.

        Args:
            workflow_args (Dict[str, Any]): Arguments for the workflow, containing
                configuration and runtime parameters.

        Example:
            >>> await activity._set_state({
            ...     "workflow_id": "123",
            ...     "metadata": {"key": "value"}
            ... })
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = ActivitiesState()

        self._state[workflow_id].workflow_args = workflow_args

    async def _get_state(self, workflow_args: Dict[str, Any]) -> ActivitiesState:
        """Retrieve the state for the current workflow.

        If state doesn't exist, it will be initialized using _set_state.

        Args:
            workflow_args: Dictionary containing workflow arguments.

        Returns:
            The state data for the current workflow.
        """
        workflow_id = get_workflow_id()
        if workflow_id not in self._state:
            await self._set_state(workflow_args)
        return self._state[workflow_id]

    async def _clean_state(self):
        """Remove the state data for the current workflow."""
        try:
            workflow_id = get_workflow_id()
            if workflow_id in self._state:
                self._state.pop(workflow_id)
        except Exception as e:
            logger.warning("Failed to clean state", exc_info=e)

    @activity.defn
    @auto_heartbeater
    async def preflight_check(self, workflow_args: Dict[str, Any]):
        """Perform preflight checks before workflow execution.

        Args:
            workflow_args: Dictionary containing workflow arguments.

        Raises:
            NotImplementedError: When not implemented by subclass.
        """
        logger.info("Starting preflight check")

        try:
            state: ActivitiesState = await self._get_state(workflow_args)
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

        except Exception as e:
            logger.error(f"Preflight check failed: {str(e)}", exc_info=True)
            raise
