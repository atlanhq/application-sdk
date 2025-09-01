"""Redis lock interceptor for Temporal workflows.

Manages distributed locks for activities decorated with @needs_lock using
separate lock acquisition and release activities to avoid workflow deadlocks.
"""

from datetime import timedelta
from typing import Any, Dict, Optional, Type

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.worker import (
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.common.error_codes import WorkflowError
from application_sdk.constants import (
    APPLICATION_NAME,
    IS_LOCKING_DISABLED,
    LOCK_METADATA_KEY,
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

    async def start_activity(  # type: ignore[override]
        self, input: StartActivityInput
    ) -> workflow.ActivityHandle[Any]:
        """Start activity with distributed lock if required."""

        # Check if activity needs locking
        activity_fn = self.activities.get(input.activity)
        if (
            not activity_fn
            or not hasattr(activity_fn, LOCK_METADATA_KEY)
            or IS_LOCKING_DISABLED
        ):
            return await self.next.start_activity(input)

        lock_config = getattr(activity_fn, LOCK_METADATA_KEY)
        lock_name = lock_config.get("lock_name", input.activity)
        max_locks = lock_config.get("max_locks", 5)
        if not input.schedule_to_close_timeout:
            logger.error(
                f"Activity '{input.activity}' with @needs_lock decorator requires schedule_to_close_timeout"
            )
            raise WorkflowError(
                f"{WorkflowError.WORKFLOW_CONFIG_ERROR}: Activity '{input.activity}' with @needs_lock decorator must be called with schedule_to_close_timeout parameter. "
                f"Example: workflow.execute_activity('{input.activity}', schedule_to_close_timeout=timedelta(minutes=10))"
            )
        ttl_seconds = int(input.schedule_to_close_timeout.total_seconds())

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
        owner_id = f"{APPLICATION_NAME}:{workflow.info().run_id}"
        lock_result = None

        try:
            # Step 1: Acquire lock via dedicated activity (can take >2s safely)
            start_to_close_timeout = workflow.info().execution_timeout
            lock_result = await workflow.execute_activity(
                "acquire_distributed_lock",
                args=[lock_name, max_locks, ttl_seconds, owner_id],
                start_to_close_timeout=start_to_close_timeout,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

            logger.debug(f"Lock acquired: {lock_result}, executing {input.activity}")

            # Step 2: Execute the business activity and return its handle
            return await self.next.start_activity(input)

        finally:
            # Step 3: Release lock (fire-and-forget with short timeout)
            if lock_result is not None:
                try:
                    await workflow.execute_local_activity(
                        "release_distributed_lock",
                        args=[lock_result["resource_id"], lock_result["owner_id"]],
                        start_to_close_timeout=timedelta(seconds=5),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                    logger.debug(f"Lock released: {lock_result['resource_id']}")
                except Exception as e:
                    # Silent failure - TTL will handle cleanup
                    logger.warning(
                        f"Lock release failed for {lock_result['resource_id']}: {e}. "
                        f"TTL will handle cleanup."
                    )
