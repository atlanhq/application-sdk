"""Redis lock interceptor for Temporal workflows.

Manages distributed locks for activities decorated with @needs_lock using
separate lock acquisition and release activities to avoid workflow deadlocks.
"""

from datetime import timedelta
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
    STRICT_LOCKING_ENABLED,
)
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

    async def start_activity(  # type: ignore
        self, input: StartActivityInput
    ) -> workflow.ActivityHandle[Any]:  # type: ignore
        """Start activity with distributed lock if required."""

        # Check if activity needs locking
        activity_fn = self.activities.get(input.activity)
        if (
            not activity_fn
            or not hasattr(activity_fn, LOCK_METADATA_KEY)
            or not STRICT_LOCKING_ENABLED
        ):
            logger.debug(
                f"Strict locking disabled, executing {input.activity} without lock"
            )
            return await self.next.start_activity(input)

        lock_config = getattr(activity_fn, LOCK_METADATA_KEY)
        lock_name = lock_config.get("lock_name", input.activity)
        max_locks = lock_config.get("max_locks", 5)
        if not input.start_to_close_timeout:
            raise ValueError("Start to close timeout is required")
        ttl_seconds = int(input.start_to_close_timeout.total_seconds())

        # Orchestrate lock acquisition -> business activity -> lock release
        return await self._execute_with_lock_orchestration(
            input, lock_name, max_locks, ttl_seconds
        )

    async def _execute_with_lock_orchestration(
        self,
        input: StartActivityInput,
        lock_name: str,
        max_locks: int,
        ttl_seconds: int,
    ) -> workflow.ActivityHandle[Any]:
        """Execute activity with distributed lock orchestration."""
        owner_id = f"{APPLICATION_NAME}:{workflow.info().workflow_id}"

        # Step 1: Acquire lock via dedicated activity (can take >2s safely)
        lock_result = await workflow.execute_activity(
            "acquire_distributed_lock",
            args=[lock_name, max_locks, ttl_seconds, owner_id],
            start_to_close_timeout=timedelta(minutes=5),
        )

        logger.debug(f"Lock acquired: {lock_result}, executing {input.activity}")

        try:
            # Step 2: Execute the business activity
            return await self.next.start_activity(input)
        finally:
            # Step 3: Release lock (fire-and-forget with short timeout)
            try:
                await workflow.execute_local_activity(
                    "release_distributed_lock",
                    args=[lock_result["resource_id"], lock_result["owner_id"]],
                    start_to_close_timeout=timedelta(seconds=5),
                )
                logger.debug(f"Lock released: {lock_result['resource_id']}")
            except Exception as e:
                # Silent failure - TTL will handle cleanup
                logger.warning(
                    f"Lock release failed for {lock_result['resource_id']}: {e}. "
                    f"TTL will handle cleanup."
                )
