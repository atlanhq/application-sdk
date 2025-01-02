import logging
from abc import ABC
from typing import Any, Callable, Dict, List

from temporalio import activity
from temporalio.client import WorkflowFailureError

from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.resources.temporal_resource import TemporalResource
from application_sdk.workflows.utils.activity import auto_heartbeater

logger = logging.getLogger(__name__)


class WorkflowInterface(ABC):
    temporal_resource: TemporalResource | None = None

    # Controllers
    preflight_check_controller: WorkflowPreflightCheckControllerInterface | None = None

    def __init__(self):
        pass

    @activity.defn
    @auto_heartbeater
    async def preflight_check(self, workflow_args: Dict[str, Any]):
        result = await self.preflight_check_controller.preflight_check(
            {
                "form_data": workflow_args["metadata"],
            }
        )
        if not result or "error" in result:
            raise ValueError("Preflight check failed")

    async def start(self, workflow_args: Any, workflow_class: Any):
        workflow_class = workflow_class or self.__class__

        try:
            if self.temporal_resource is None:
                raise ValueError("Temporal resource is not set")

            return await self.temporal_resource.start_workflow(
                workflow_args=workflow_args, workflow_class=workflow_class
            )
        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "WorkflowInterface":
        self.temporal_resource = temporal_resource
        return self

    def set_preflight_check_controller(
        self, preflight_check_controller: WorkflowPreflightCheckControllerInterface
    ) -> "WorkflowInterface":
        self.preflight_check_controller = preflight_check_controller
        return self

    def get_activities(self) -> List[Callable[..., Any]]:
        return []

    async def run(self, workflow_config: Dict[str, Any]):
        pass
