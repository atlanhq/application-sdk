from abc import ABC
from datetime import timedelta
from typing import Any, Callable, Dict, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.inputs.statestore import StateStore


@workflow.defn
class WorkflowInterface(ABC):
    activities_cls: Type[ActivitiesInterface]

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        raise NotImplementedError("Workflow get_activities method not implemented")

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        # TODO: Remove logic if not needed based on preflight check decisions
        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStore.extract_configuration(workflow_id)

        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        await workflow.execute_activity_method(
            self.activities_cls.preflight_check,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )
