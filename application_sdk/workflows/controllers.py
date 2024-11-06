from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List

from temporalio.client import WorkflowFailureError

from application_sdk.logging import get_logger
from application_sdk.workflows.resources import TemporalResource

logger = get_logger(__name__)


# Controller base class
class WorkflowControllerInterface(ABC):
    pass


class WorkflowAuthControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow auth Controllers
    """

    @abstractmethod
    def test_auth(self, credential: Dict[str, Any]) -> bool:
        raise NotImplementedError


class WorkflowMetadataControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow metadata Controllers
    """

    @abstractmethod
    async def fetch_metadata(self) -> List[Dict[str, str]]:
        raise NotImplementedError


class WorkflowPreflightCheckControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow preflight check Controllers
    """

    @abstractmethod
    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class WorkflowWorkerControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow workers

    This class provides a default implementation for the workflow, with hooks
    for subclasses to customize specific behaviors.
    """

    temporal_resource: TemporalResource
    temporal_activities: List[Callable]

    def __init__(
        self, temporal_resource: TemporalResource, temporal_activities: List[Callable]
    ):
        self.temporal_resource = temporal_resource

        self.temporal_worker = None
        self.temporal_activities = temporal_activities

    @abstractmethod
    async def run(self, *args: Any, **kwargs: Any) -> None:
        """
        Run the workflow

        This method defines the workflow execution logic with references to the
        activities and child workflows that are defined in the subclass.
        """
        raise NotImplementedError

    async def start_workflow(self, workflow_args: Any) -> Dict[str, Any]:
        """
        Start the workflow
        This method is the request handler/client that starts the workflow.

        Args:
            workflow_args: The arguments to pass to the workflow.

        Returns:
            The workflow execution metadata.
        """
        try:
            return await self.temporal_resource.start_workflow(
                workflow_args=workflow_args
            )
        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e

    async def start_worker(self):
        """
        Start the worker
        """

        temporal_worker = await self.temporal_resource.create_worker(
            self.temporal_activities
        )

        logger.info(f"Starting worker with task queue: {temporal_worker.task_queue}")
        await temporal_worker.run()
