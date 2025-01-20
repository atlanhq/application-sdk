from abc import ABC, abstractmethod
from typing import Any, Dict

from application_sdk.activities.common.utils import get_workflow_id


class ActivitiesInterface(ABC):
    """Abstract base class defining the interface for workflow activities.

    This class provides state management functionality and defines the basic structure
    for activity implementations.
    """

    def __init__(self):
        """Initialize the activities interface with an empty state dictionary."""
        self._state: Dict[str, Any] = {}

    # State methods
    @abstractmethod
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Set the state for the current workflow.

        Args:
            workflow_args: Dictionary containing workflow arguments.

        Raises:
            NotImplementedError: When not implemented by subclass.
        """
        raise NotImplementedError("_set_state not implemented")

    async def _get_state(self, workflow_args: Dict[str, Any]) -> Any:
        """Retrieve the state for the current workflow.

        If state doesn't exist, it will be initialized using _set_state.

        Args:
            workflow_args: Dictionary containing workflow arguments.

        Returns:
            The state data for the current workflow.
        """
        if get_workflow_id() not in self._state:
            await self._set_state(workflow_args)
        return self._state[get_workflow_id()]

    async def _clean_state(self):
        """Remove the state data for the current workflow."""
        self._state.pop(get_workflow_id())

    # TODO: Try it out
    @abstractmethod
    async def preflight_check(self, workflow_args: Dict[str, Any]) -> None:
        """Perform preflight checks before workflow execution.

        Args:
            workflow_args: Dictionary containing workflow arguments.

        Raises:
            NotImplementedError: When not implemented by subclass.
        """
        raise NotImplementedError("Preflight check not implemented")
