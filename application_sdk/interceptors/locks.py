import asyncio
import random
from typing import Any, Dict, Optional, Type

from dapr.clients import DaprClient
from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.constants import (
    APPLICATION_NAME,
    LOCK_METADATA_KEY,
    LOCK_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LockWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    """Interceptor that manages distributed locks for activities."""

    def __init__(self, next: WorkflowOutboundInterceptor):
        super().__init__(next)
        self.activities = {}

    def set_activities(self, activities: Dict[str, Any]) -> None:
        """Set the activities dictionary for metadata lookup."""
        self.activities = activities

    async def start_activity(
        self, input: StartActivityInput
    ) -> workflow.ActivityHandle[Any]:
        # Check if activity needs locking using metadata
        activity_fn = self.activities.get(input.activity)
        if not activity_fn or not hasattr(activity_fn, LOCK_METADATA_KEY):
            return await self.next.start_activity(input)

        # Quick check for lockstore component
        try:
            with DaprClient() as client:
                metadata = client.get_metadata()
                components = metadata.registered_components
                if not any(comp.name == LOCK_STORE_NAME for comp in components):
                    logger.warning(
                        f"Dapr component {LOCK_STORE_NAME} is not available, skipping lock acquisition, please use dapr lock component for testing locks locally"
                    )
                    return await self.next.start_activity(input)
        except Exception as e:
            logger.warning(f"Failed to check Dapr components: {e}")
            return await self.next.start_activity(input)

        lock_config = getattr(activity_fn, LOCK_METADATA_KEY)
        # Get lock configuration
        lock_name = lock_config.get("lock_name", input.activity)
        max_locks = lock_config.get("max_locks", 5)
        start_to_close_timeout = (
            int(input.start_to_close_timeout.total_seconds())
            if input.start_to_close_timeout
            else 300
        )

        # Try to acquire a lock before starting the activity
        while True:
            slot = random.randint(0, max_locks - 1)
            lock_id = f"{APPLICATION_NAME}:{lock_name}:{slot}"
            owner_id = f"{APPLICATION_NAME}:{workflow.info().workflow_id}"

            try:
                with DaprClient() as client:
                    with client.try_lock(
                        store_name=LOCK_STORE_NAME,
                        resource_id=lock_id,
                        lock_owner=owner_id,
                        expiry_in_seconds=start_to_close_timeout,
                    ) as lock:
                        if lock.success:
                            logger.debug(
                                f"Lock acquired {slot}, starting activity {input.activity}"
                            )
                            return await self.next.start_activity(input)
            except Exception as e:
                logger.error(
                    f"Failed to acquire lock for activity '{input.activity}'. Possible Dapr connectivity issue. Error: {e}"
                )
                raise e

            await workflow.sleep(random.uniform(0, 0.5))
            logger.debug(
                f"No lock for slot {slot}, retrying for activity {input.activity}"
            )


class LockWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Inbound interceptor that sets up the lock outbound interceptor."""

    def __init__(self, next: Optional[WorkflowInboundInterceptor] = None):
        self.activities = {}
        super().__init__(next)

    def set_activities(self, activities: Dict[str, Any]) -> None:
        """Set the activities dictionary for metadata lookup."""
        self.activities = activities

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        lock_interceptor = LockWorkflowOutboundInterceptor(outbound)
        lock_interceptor.set_activities(self.activities)
        self.next.init(lock_interceptor)


class LockInterceptor(Interceptor):
    """Main interceptor class that Temporal uses to create the interceptor chain."""

    def __init__(self, activities: Dict[str, Any]):
        self.activities = activities
        super().__init__()

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        # Create a new class that inherits from LockWorkflowInboundInterceptor
        activities = self.activities

        class ConfiguredLockWorkflowInboundInterceptor(LockWorkflowInboundInterceptor):
            def __init__(self, next: Optional[WorkflowInboundInterceptor] = None):
                super().__init__(next)
                self.set_activities(activities)

        return ConfiguredLockWorkflowInboundInterceptor
