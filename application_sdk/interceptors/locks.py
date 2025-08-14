import random
from typing import Any, Dict, Optional, Type

from temporalio import workflow
from temporalio.worker import (
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.constants import (
    APPLICATION_NAME,
    LOCK_METADATA_KEY,
    REDIS_PASSWORD,
    REDIS_SENTINEL_HOSTS,
    REDIS_SERVICE_NAME,
)
from application_sdk.locks.redis_lock import RedisLockProvider
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LockWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    """Interceptor that manages distributed locks for activities."""

    def __init__(self, next: WorkflowOutboundInterceptor):
        super().__init__(next)
        self.activities = {}
        self._lock_provider: Optional[RedisLockProvider] = None

    def set_activities(self, activities: Dict[str, Any]) -> None:
        """Set the activities dictionary for metadata lookup."""
        self.activities = activities

    async def _get_lock_provider(self) -> RedisLockProvider:
        """Get or create the lock provider instance."""
        if not self._lock_provider:
            # Parse sentinel hosts
            hosts = [
                (h.split(":")[0], int(h.split(":")[1]) if ":" in h else 26379)
                for h in REDIS_SENTINEL_HOSTS
            ]
            self._lock_provider = RedisLockProvider(
                sentinel_hosts=hosts,
                service_name=REDIS_SERVICE_NAME,
                password=REDIS_PASSWORD,
            )
            await self._lock_provider.initialize()
        return self._lock_provider

    async def start_activity(
        self, input: StartActivityInput
    ) -> workflow.ActivityHandle[Any]:
        # Check if activity needs locking using metadata
        activity_fn = self.activities.get(input.activity)
        if not activity_fn or not hasattr(activity_fn, LOCK_METADATA_KEY):
            return await self.next.start_activity(input)

        # Quick check for lock provider availability
        try:
            lock_provider = await self._get_lock_provider()
            if not await lock_provider.is_available():
                logger.warning(
                    "Lock provider is not available, skipping lock acquisition"
                )
                return await self.next.start_activity(input)
        except Exception as e:
            logger.warning(f"Failed to check lock provider availability: {e}")
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
        lock_provider = await self._get_lock_provider()

        while True:
            slot = random.randint(0, max_locks - 1)
            lock_id = f"{APPLICATION_NAME}:{lock_name}:{slot}"
            owner_id = f"{APPLICATION_NAME}:{workflow.info().workflow_id}"

            try:
                async with lock_provider.try_lock(
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
                    f"Failed to acquire lock for activity '{input.activity}'. Error: {e}"
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
