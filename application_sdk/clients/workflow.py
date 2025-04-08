from enum import Enum
from typing import Any, Dict, Optional, Sequence, Type
from abc import ABC, abstractmethod

from application_sdk.workflows import WorkflowInterface


class WorkflowEngineType(Enum):
    TEMPORAL = "temporal"
    DAPR = "dapr"


class WorkflowClient(ABC):
    """Abstract base class defining workflow operations independent of technology."""

    @abstractmethod
    async def load(self) -> None:
        """Initialize the workflow client connection."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the workflow client connection."""
        pass

    @abstractmethod
    async def start_workflow(
        self, 
        workflow_args: Dict[str, Any], 
        workflow_class: Type['WorkflowInterface']
    ) -> Dict[str, Any]:
        """Start a workflow execution.

        Args:
            workflow_args: Arguments for the workflow
            workflow_class: The workflow class to execute

        Returns:
            Dict containing workflow_id, run_id and other implementation-specific details
        """
        pass

    @abstractmethod
    async def stop_workflow(self, workflow_id: str, run_id: str) -> None:
        """Stop a workflow execution.

        Args:
            workflow_id: The ID of the workflow
            run_id: The run ID of the workflow
        """
        pass

    @abstractmethod
    async def get_workflow_run_status(
        self,
        workflow_id: str,
        run_id: Optional[str] = None,
        include_last_executed_run_id: bool = False,
    ) -> Dict[str, Any]:
        """Get the status of a workflow run.

        Args:
            workflow_id: The workflow ID
            run_id: Optional run ID
            include_last_executed_run_id: Whether to include the last executed run ID

        Returns:
            Dict containing status information
        """
        pass

    @abstractmethod
    def create_worker(
        self,
        activities: Sequence[Any],
        workflow_classes: Sequence[Any],
        passthrough_modules: Sequence[str],
        max_concurrent_activities: Optional[int] = None,
    ) -> Any:
        """Create a workflow worker.

        Args:
            activities: Activity functions to register
            workflow_classes: Workflow classes to register
            passthrough_modules: Modules to pass through to worker
            max_concurrent_activities: Maximum concurrent activities

        Returns:
            Worker instance specific to the implementation
        """
        pass
