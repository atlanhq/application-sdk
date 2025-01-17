from abc import ABC
from typing import Type, Sequence, Callable, Any, Dict, Optional
from temporalio import workflow
from application_sdk.activities import ActivitiesInterface

@workflow.defn
class WorkflowInterface(ABC):
    activities_cls: Type[ActivitiesInterface]

    def __init__(
        self,
        activities_cls: Type[ActivitiesInterface]
    ):
        self.activities_cls = activities_cls

    @workflow.run
    async def run(self, _: Dict[str, Any]) -> None:
        raise NotImplementedError("Workflow run method not implemented")
    
    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        raise NotImplementedError("Workflow get_activities method not implemented")
