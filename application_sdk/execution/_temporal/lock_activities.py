"""Lock management activities for distributed locking.

These activities handle the actual Redis lock acquisition and release,
allowing the workflow to orchestrate locking without hitting Temporal's
deadlock timeout.
"""

import random
from typing import Any

from temporalio import activity

from application_sdk.clients.redis import RedisClientAsync
from application_sdk.errors import AppError
from application_sdk.execution._temporal._lock_errors import (
    LockAcquisitionError,
    MaxLocksInvalidError,
    RedisLockError,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@activity.defn
async def acquire_distributed_lock(
    lock_name: str,
    max_locks: int,
    ttl_seconds: int = 100,
    owner_id: str = "default_owner",
) -> dict[str, Any]:
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
        AppError: If lock acquisition fails due to Redis errors or invalid parameters.
    """
    # Input validation
    if max_locks <= 0:
        raise MaxLocksInvalidError(
            message=f"max_locks must be greater than 0, got {max_locks}",
            value_summary=str(max_locks),
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
                logger.info("Lock acquired: slot=%s resource_id=%s", slot, resource_id)
                return {
                    "status": True,
                    "slot_id": slot,
                    "resource_id": resource_id,
                    "owner_id": owner_id,
                }

            raise LockAcquisitionError()
    # conformance: ignore[E004] top-level error boundary; always re-raises as AppError or typed RedisLockError
    except Exception as e:
        if isinstance(e, AppError):
            raise
        raise RedisLockError(
            message=f"Redis error during lock acquisition for {resource_id}",
            network_error=type(e).__name__,
        ) from e


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
                    "Lock released successfully: resource_id=%s result=%s",
                    resource_id,
                    result.value,
                )
            return released

    except Exception:
        logger.error("Redis error during lock release: %s", resource_id, exc_info=True)
        # Don't raise exception for lock release failures - log and return False
        # Lock release is best-effort and shouldn't fail the workflow
        return False
