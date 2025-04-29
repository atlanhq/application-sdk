import asyncio
from typing import Any, Callable, Dict, Sequence

from temporalio import activity, workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.app import WorkflowApp
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workflows import WorkflowInterface

APPLICATION_NAME = "hello-world"

logger = get_logger(__name__)


@workflow.defn
class HelloWorldWorkflow(WorkflowInterface):
    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        print("HELLO WORLD")

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        return []


class HelloWorldActivities(ActivitiesInterface):
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        return

    @activity.defn
    @auto_heartbeater
    async def preflight_check(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        return {"message": "Preflight check completed successfully"}


async def application_hello_world(daemon: bool = True) -> Dict[str, Any]:
    logger.info("Starting application_hello_world")

    activities = HelloWorldActivities()

    app = WorkflowApp(
        application_name=APPLICATION_NAME,
        workflow_classes=[HelloWorldWorkflow],
        activities=activities,
        workflow_args={},  # No args for hello world
    )
    return await app.run(HelloWorldWorkflow, daemon=daemon)


if __name__ == "__main__":
    asyncio.run(application_hello_world(daemon=False))
