from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Type

from application_sdk.workflows import WorkflowInterface


class WorkflowEngineType(Enum):
    TEMPORAL = "temporal"
    DAPR = "dapr"


class WorkflowClient(ABC):
    """Abstract base class for workflow client implementations.

    This class defines the interface for interacting with workflow execution systems.
    It provides methods for managing workflow lifecycles including starting,
    stopping, and monitoring workflow executions. Implementations should handle
    the specifics of connecting to and interacting with particular workflow
    execution systems.
    """

    @abstractmethod
    async def load(self) -> None:
        """Initialize and establish the workflow client connection.

        This method should handle any necessary setup and authentication to
        establish a connection with the workflow execution system.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            ConnectionError: If connection establishment fails.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the workflow client connection.

        This method should properly terminate the connection to the workflow
        execution system and clean up any resources.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        pass

    @abstractmethod
    async def start_workflow(
        self, workflow_args: Dict[str, Any], workflow_class: Type["WorkflowInterface"]
    ) -> Dict[str, Any]:
        """Start a new workflow execution.

        This method initiates a new workflow execution with the provided arguments
        and workflow class. It should handle workflow instantiation and execution
        setup.

        Args:
            workflow_args (Dict[str, Any]): Arguments to pass to the workflow.
                Must contain all required parameters for the workflow.
            workflow_class (Type[WorkflowInterface]): The class of the workflow
                to execute.

        Returns:
            Dict[str, Any]: Information about the started workflow, including at
                minimum:
                - workflow_id: Unique identifier for the workflow
                - run_id: Unique identifier for this execution

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            ValueError: If required arguments are missing or invalid.
        """
        pass

    @abstractmethod
    async def stop_workflow(self, workflow_id: str, run_id: str) -> None:
        """Stop a running workflow execution.

        This method terminates a running workflow execution. The exact behavior
        (e.g., graceful shutdown vs immediate termination) may vary by
        implementation.

        Args:
            workflow_id (str): The ID of the workflow to stop.
            run_id (str): The specific run ID of the workflow execution.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            ValueError: If the workflow or run ID is invalid.
            RuntimeError: If the workflow cannot be stopped.
        """
        pass

    @abstractmethod
    async def get_workflow_run_status(
        self,
        workflow_id: str,
        run_id: Optional[str] = None,
        include_last_executed_run_id: bool = False,
    ) -> Dict[str, Any]:
        """Get the current status of a workflow run.

        This method retrieves the current status and other relevant information
        about a workflow execution.

        Args:
            workflow_id (str): The ID of the workflow to check.
            run_id (Optional[str], optional): The specific run ID to check.
                If None, checks the latest run. Defaults to None.
            include_last_executed_run_id (bool, optional): Whether to include
                the ID of the last executed run in the response. Defaults to False.

        Returns:
            Dict[str, Any]: Status information about the workflow run, including
                at minimum:
                - status: Current execution status
                - start_time: When the workflow started
                - end_time: When the workflow ended (if completed)
                - error: Error information (if failed)

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            ValueError: If the workflow or run ID is invalid.
        """
        pass

    @abstractmethod
    def create_worker(
        self,
        activities: Sequence[Any],
        workflow_classes: Sequence[Any],
        passthrough_modules: Sequence[str],
        max_concurrent_activities: Optional[int] = None,
        activity_executor: Optional[ThreadPoolExecutor] = None,
    ) -> Any:
        """Create a worker for executing workflow activities.

        This method creates a worker instance that can execute the specified
        workflow activities and classes.

        Args:
            activities (Sequence[Any]): Activity functions to register with
                the worker.
            workflow_classes (Sequence[Any]): Workflow classes to register with
                the worker.
            passthrough_modules (Sequence[str]): Names of modules that should be
                made available to the worker.
            max_concurrent_activities (Optional[int], optional): Maximum number
                of activities that can run concurrently. None means no limit.
                Defaults to None.
            activity_executor (ThreadPoolExecutor | None): Executor for running activities.

        Returns:
            Any: A worker instance specific to the implementation.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            ValueError: If invalid activities or workflow classes are provided.
        """
        pass
