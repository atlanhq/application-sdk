"""Lock management activities for distributed locking.

These activities handle the actual Redis lock acquisition and release,
allowing the workflow to orchestrate locking without hitting Temporal's
deadlock timeout.
"""

import asyncio
import random
from typing import Any, Dict

from temporalio import activity

from application_sdk.clients.redis import get_redis_client
from application_sdk.constants import APPLICATION_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@activity.defn
async def acquire_distributed_lock(
    lock_name: str, max_locks: int, ttl_seconds: int, owner_id: str
) -> Dict[str, Any]:
    """Acquire a distributed lock with retry logic.

    Args:
        lock_name: Name of the resource to lock
        max_locks: Maximum number of concurrent locks allowed
        ttl_seconds: Time-to-live for the lock in seconds
        owner_id: Unique identifier for the lock owner

    Returns:
        Dictionary containing lock information:
        {
            "slot_id": int,
            "resource_id": str,
            "owner_id": str
        }

    Raises:
        RuntimeError: If lock acquisition fails due to Redis errors
    """
    redis_client = get_redis_client()

    while True:
        slot = random.randint(0, max_locks - 1)
        resource_id = f"{APPLICATION_NAME}:{lock_name}:{slot}"

        try:
            # Acquire lock directly without context manager to keep it held
            acquired = redis_client._acquire_lock(resource_id, owner_id, ttl_seconds)
            if acquired:
                logger.info(f"Lock acquired for slot {slot}, resource: {resource_id}")
                return {
                    "slot_id": slot,
                    "resource_id": resource_id,
                    "owner_id": owner_id,
                }

            # Health check after failed acquisition
            if not redis_client.health_check():
                raise RuntimeError("Redis health check failed during lock acquisition")

        except Exception as e:
            # In strict mode: always fail on Redis errors
            raise RuntimeError(f"Redis error during lock acquisition: {e}")

        # Wait before retrying
        await asyncio.sleep(random.uniform(0.1, 0.5))


@activity.defn
async def release_distributed_lock(resource_id: str, owner_id: str) -> bool:
    """Release a distributed lock.

    Args:
        resource_id: Full resource identifier for the lock
        owner_id: Unique identifier for the lock owner

    Returns:
        True if lock was released successfully, False otherwise
    """
    redis_client = get_redis_client()

    try:
        released, reason = redis_client._release_lock(resource_id, owner_id)
        if released:
            logger.info(f"Lock released successfully: {resource_id}")
        else:
            logger.warning(f"Lock release failed: {resource_id}, reason: {reason}")
        return released

    except Exception as e:
        logger.error(f"Error releasing lock {resource_id}: {e}")
        return False
