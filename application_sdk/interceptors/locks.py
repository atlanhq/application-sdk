import asyncio
import random
from typing import Any

from dapr.clients import DaprClient
from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from application_sdk.constants import (
    APPLICATION_NAME,
    LOCK_METADATA_KEY,
    LOCK_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LockActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor that manages distributed locks for activities when they are picked up by workers."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        # Check if activity needs locking using metadata
        if not hasattr(input.fn, LOCK_METADATA_KEY):
            return await super().execute_activity(input)

        logger.debug(f"Attempting to acquire lock for activity: {input.fn.__name__}")

        # Quick check for lockstore component
        try:
            with DaprClient() as client:
                metadata = client.get_metadata()
                components = metadata.registered_components
                if not any(comp.name == LOCK_STORE_NAME for comp in components):
                    logger.error(
                        f"Dapr component {LOCK_STORE_NAME} is not available, skipping lock acquisition, please use dapr lock component for testing locks locally"
                    )
                    raise Exception(
                        f"Dapr component {LOCK_STORE_NAME} is not available, skipping lock acquisition, please use dapr lock component for testing locks locally"
                    )
        except Exception as e:
            logger.error(f"Failed to check Dapr components: {e}")
            raise e

        lock_config = getattr(input.fn, LOCK_METADATA_KEY)
        # Get lock configuration
        lock_name = lock_config.get("lock_name", input.fn.__name__)
        max_locks = lock_config.get("max_locks", 5)

        # Use activity timeout or default to 300 seconds
        activity_timeout = 300
        activity_info = activity.info()
        if activity_info.start_to_close_timeout is not None:
            activity_timeout = int(activity_info.start_to_close_timeout.total_seconds())

        while True:
            slot = random.randint(0, max_locks - 1)
            lock_id = f"{APPLICATION_NAME}:{lock_name}:{slot}"
            owner_id = f"{APPLICATION_NAME}:{input.fn.__name__}:{activity_info.workflow_run_id}:{activity_info.activity_id}"

            logger.debug(
                f"Attempting to acquire lock {lock_id} for activity {input.fn.__name__}"
            )

            try:
                with DaprClient() as client:
                    with client.try_lock(
                        store_name=LOCK_STORE_NAME,
                        resource_id=lock_id,
                        lock_owner=owner_id,
                        expiry_in_seconds=activity_timeout,
                    ) as lock:
                        if lock.success:
                            logger.debug(
                                f"Lock acquired {slot} for activity {input.fn.__name__}, executing activity"
                            )
                            try:
                                # Execute activity with lock held
                                result = await super().execute_activity(input)
                                logger.debug(
                                    f"Activity {input.fn.__name__} completed successfully"
                                )
                                return result
                            except Exception as e:
                                logger.error(
                                    f"Activity {input.fn.__name__} failed: {e}"
                                )
                                raise
                            finally:
                                # Lock is automatically released when with block ends
                                logger.debug(
                                    f"Lock released for activity {input.fn.__name__}"
                                )
                        else:
                            logger.debug(
                                f"Failed to acquire lock for slot {slot}, retrying..."
                            )
                            await asyncio.sleep(random.uniform(10, 20))
            except Exception as e:
                logger.error(
                    f"Failed to acquire lock for activity {input.fn.__name__}: {e}"
                )
                raise e


class LockInterceptor(Interceptor):
    """Main interceptor class that Temporal uses to create the interceptor chain."""

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions.

        Args:
            next (ActivityInboundInterceptor): The next interceptor in the chain.

        Returns:
            ActivityInboundInterceptor: The activity interceptor.
        """
        return LockActivityInboundInterceptor(super().intercept_activity(next))
