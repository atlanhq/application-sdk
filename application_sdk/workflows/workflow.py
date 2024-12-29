import logging
from abc import ABC
from typing import Any, Callable, List

from temporalio import workflow
from temporalio.client import WorkflowFailureError

from application_sdk.workflows.resources.temporal_resource import TemporalResource, TemporalConfig
from application_sdk.workflows.resources.constants import TemporalConstants

# from application_sdk.workflows.workflow import WorkflowBuilderInterface

logger = logging.getLogger(__name__)


class WorkflowInterface(ABC):
    temporal_resource: TemporalResource | None = None

    def __init__(self):
        pass

    async def start(self, workflow_args: Any, workflow_class: Any | None = None):
        workflow_class = workflow_class or self.__class__

        try:
            if self.temporal_resource is None:
                temporal_resource = TemporalResource(
                    TemporalConfig(
                        application_name=TemporalConstants.APPLICATION_NAME.value,
                    )
                )
                await temporal_resource.load()
                self.set_temporal_resource(temporal_resource)

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

    def get_activities(self) -> List[Callable[..., Any]]:
        return []

    async def run(self, workflow_args: Any):
        pass


# @workflow.defn
# class SampleWorkflow(WorkflowInterface):

#     # @activity.defn
#     # async def activity_1(self, workflow_args: Any):
#     #     print("Activity 1")
#     #     pass

#     # @activity.defn
#     # async def activity_2(self, workflow_args: Any):
#     #     print("Activity 2")
#     #     pass

#     @workflow.run
#     async def run(self, workflow_args: Any):
#         pass

#     async def start(self, workflow_args: Any, workflow_class: Any):
#         return await super().start(workflow_args, self.__class__)


# class SampleWorkflowBuilder(WorkflowBuilderInterface):
#     temporal_resource: TemporalResource
#     def build(self, workflow: SampleWorkflow | None = None) -> SampleWorkflow:
#         return SampleWorkflow().set_temporal_resource(self.temporal_resource)
