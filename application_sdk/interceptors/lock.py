"""Redis lock interceptor for Temporal workflows.

Manages distributed locks for activities decorated with @needs_lock using
the global Redis client connected at app startup.
"""

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

from application_sdk.constants import APPLICATION_NAME, LOCK_METADATA_KEY
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class RedisLockInterceptor(Interceptor):
    """Main interceptor class for Redis distributed locking."""

    def __init__(self, activities: Dict[str, Any]):
        """Initialize Redis lock interceptor.

        Args:
            activities: Dictionary mapping activity names to activity functions
        """
        self.activities = activities

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        activities = self.activities

        class RedisLockWorkflowInboundInterceptor(WorkflowInboundInterceptor):
            """Inbound interceptor that manages Redis locks for activities."""

            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                """Initialize with Redis lock outbound interceptor."""
                lock_outbound = RedisLockOutboundInterceptor(outbound, activities)
                super().init(lock_outbound)

        return RedisLockWorkflowInboundInterceptor


class RedisLockOutboundInterceptor(WorkflowOutboundInterceptor):
    """Outbound interceptor that acquires Redis locks before activity execution."""

    def __init__(self, next: WorkflowOutboundInterceptor, activities: Dict[str, Any]):
        super().__init__(next)
        self.activities = activities

    async def start_activity(
        self, input: StartActivityInput
    ) -> workflow.ActivityHandle[Any]:  # type: ignore
        """Start activity with distributed lock if required."""

        # Check if activity needs locking
        activity_fn = self.activities.get(input.activity)
        if not activity_fn or not hasattr(activity_fn, LOCK_METADATA_KEY):
            return await self.next.start_activity(input)

        # Get lock configuration
        lock_config = getattr(activity_fn, LOCK_METADATA_KEY)
        lock_name = lock_config.get("lock_name", input.activity)
        max_locks = lock_config.get("max_locks", 5)
        ttl_seconds = (
            int(input.start_to_close_timeout.total_seconds())
            if input.start_to_close_timeout
            else 300
        )

        # Get Redis client
        from application_sdk.clients.redis import get_redis_client

        redis_client = get_redis_client()

        # Skip locking if Redis unavailable
        if not redis_client.connected:
            logger.debug(f"Redis unavailable, executing {input.activity} without lock")
            return await self.next.start_activity(input)

        # Try to acquire lock with semaphore pattern
        return await self._execute_with_lock(
            input, lock_name, max_locks, ttl_seconds, redis_client
        )

    async def _execute_with_lock(
        self,
        input: StartActivityInput,
        lock_name: str,
        max_locks: int,
        ttl_seconds: int,
        redis_client: Any,
    ) -> workflow.ActivityHandle[Any]:
        """Execute activity with distributed lock acquisition."""

        owner_id = f"{APPLICATION_NAME}:{workflow.info().workflow_id}"

        # Try lock slots with jitter
        while True:
            slot = workflow.random().randint(0, max_locks - 1)
            resource_id = f"{APPLICATION_NAME}:{lock_name}:{slot}"

            # Attempt lock acquisition
            with redis_client.lock(resource_id, owner_id, ttl_seconds) as acquired:
                if acquired:
                    logger.debug(
                        f"Lock acquired for slot {slot}, executing {input.activity}"
                    )
                    return await self.next.start_activity(input)

                logger.debug(f"Lock unavailable for slot {slot}")

            # Sleep with jitter before retry
            await workflow.sleep(workflow.random().uniform(0, 0.5))
