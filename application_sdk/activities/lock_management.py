"""Lock management activities for distributed locking.

These activities handle the actual Redis lock acquisition and release,
allowing the workflow to orchestrate locking without hitting Temporal's
deadlock timeout.
"""

import random
from typing import Any, Dict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from application_sdk.clients.redis import RedisClientAsync
from application_sdk.common.error_codes import ActivityError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@activity.defn
async def acquire_distributed_lock(
    lock_name: str,
    max_locks: int,
    ttl_seconds: int = 100,
    owner_id: str = "default_owner",
) -> Dict[str, Any]:
    """Acquire a distributed lock with retry logic.

    Args:
        lock_name (str): Name of the resource to lock.
        max_locks (int): Maximum number of concurrent locks allowed; must be > 0.
        ttl_seconds (int): Time-to-live for the lock in seconds.
        owner_id (str): Unique identifier for the lock owner.

    Returns:
        dict[str, Any]: Lock information with the following keys:
            - slot_id (int): Allocated lock slot.
            - resource_id (str): Fully qualified lock resource ID.
            - owner_id (str): Owner identifier.

    Raises:
        ActivityError: If lock acquisition fails due to Redis errors or invalid parameters.
    """
    # Input validation
    if max_locks <= 0:
        raise ApplicationError(
            f"{ActivityError.LOCK_ACQUISITION_ERROR}: max_locks must be greater than 0, got {max_locks}",
            non_retryable=True,
        )
    slot = random.randint(0, max_locks - 1)
    resource_id = f"{lock_name}:{slot}"

    try:
        async with RedisClientAsync() as redis_client:
            # Acquire lock - connection will stay open until context exits
            acquired = await redis_client._acquire_lock(
                resource_id, owner_id, ttl_seconds
            )
            if acquired:
                logger.info(f"Lock acquired for slot {slot}, resource: {resource_id}")
                return {
                    "status": True,
                    "slot_id": slot,
                    "resource_id": resource_id,
                    "owner_id": owner_id,
                }

            raise ActivityError(
                f"{ActivityError.LOCK_ACQUISITION_ERROR}: Lock not acquired for {resource_id}, will retry after some time"
            )
    except Exception as e:
        # Redis connection or operation failed - propagate as activity error
        if isinstance(e, (ActivityError)):
            raise e
        logger.error(f"Redis error during lock acquisition: {e}")
        raise ApplicationError(
            f"Redis error during lock acquisition for {resource_id}, error: {e}",
            non_retryable=True,
            type=type(e).__name__,
        )


@activity.defn
async def release_distributed_lock(
    resource_id: str, owner_id: str = "default_owner"
) -> bool:
    """Release a distributed lock.

    Args:
        resource_id: Full resource identifier for the lock
        owner_id: Unique identifier for the lock owner

    Returns:
        True if lock was released successfully, False otherwise
    """
    try:
        async with RedisClientAsync() as redis_client:
            released, result = await redis_client._release_lock(resource_id, owner_id)
            if released:
                logger.info(
                    f"Lock released successfully: {resource_id}, result: {result.value}"
                )
            return released

    except Exception as e:
        logger.error(f"Redis error during lock release for {resource_id}: {e}")
        # Don't raise exception for lock release failures - log and return False
        # Lock release is best-effort and shouldn't fail the workflow
        return False
