import asyncio
from datetime import timedelta
from typing import Any, Callable, Dict, Sequence, cast

from temporalio import activity, workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.application import BaseApplication
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface

APPLICATION_NAME = "hello-world"

logger = get_logger(__name__)


@workflow.defn
class HelloWorldWorkflow(WorkflowInterface):
    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        activities = HelloWorldActivities()

        await workflow.execute_activity_method(
            activities.demo_activity,
            args=[workflow_config],
            start_to_close_timeout=timedelta(seconds=10),
            heartbeat_timeout=timedelta(seconds=10),
        )

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        activities = cast(HelloWorldActivities, activities)
        return [
            activities.demo_activity,
        ]


class HelloWorldActivities(ActivitiesInterface):
    @activity.defn
    @auto_heartbeater
    async def demo_activity(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        return {"message": "Demo activity completed successfully"}


async def application_hello_world(daemon: bool = True) -> Dict[str, Any]:
    logger.info("Starting application_hello_world")

    # initialize application
    app = BaseApplication(name=APPLICATION_NAME)

    # setup workflow
    await app.setup_workflow(
        workflow_and_activities_classes=[(HelloWorldWorkflow, HelloWorldActivities)]
    )

    # start workflow
    workflow_response = await app.start_workflow(
        workflow_args={"workflow_id": "hello-world"}, workflow_class=HelloWorldWorkflow
    )

    # start worker
    await app.start_worker(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_hello_world(daemon=False))
