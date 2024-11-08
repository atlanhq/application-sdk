from abc import ABC, abstractmethod
from typing import List, Callable, Any
from application_sdk.workflows.resources import TemporalResource
from temporalio.client import WorkflowFailureError

import logging

logger = logging.getLogger(__name__)

class WorkflowInterface(ABC):
    temporal_resource: TemporalResource | None = None

    def __init__(self):
        pass

    async def start(self, workflow_args: Any, workflow_class: Any):
        workflow_class = workflow_class or self.__class__

        try:
            if self.temporal_resource is None:
                raise ValueError("Temporal resource is not set")

            return await self.temporal_resource.start_workflow(
                workflow_args=workflow_args,
                workflow_class=workflow_class
            )
        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e


    def set_temporal_resource(self, temporal_resource: TemporalResource) -> "WorkflowInterface":
        self.temporal_resource = temporal_resource
        return self
    
    def get_activities(self) -> List[Callable[..., Any]]:
        return []
