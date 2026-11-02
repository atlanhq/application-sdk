"""Lock management activities for distributed locking.

These activities handle the actual Redis lock acquisition and release,
allowing the workflow to orchestrate locking without hitting Temporal's
deadlock timeout.
"""

import uuid
import zlib
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


def _acquire_token(owner_id: str) -> str:
    """Return the lock value identifying *this* acquire across its retries.

    ``owner_id`` names the workflow run, which may hold several slots of one
    lock at once (parallel locked activities), so it cannot tell this acquire's
    slot apart from a sibling's. The acquire activity's ``activity_id`` is
    stable across Temporal retries of one scheduled activity and distinct
    between scheduled activities, so ``owner_id:activity_id`` is.

    Outside an activity (a direct call) there is no retry to dedupe against,
    so each call gets a fresh token: two direct acquires under one
    ``owner_id`` must take two slots, not collapse into one.
    """
    try:
        activity_id = activity.info().activity_id
    except RuntimeError:
        return f"{owner_id}:{uuid.uuid4()}"
    return f"{owner_id}:{activity_id}"


async def _scan_slots(
    redis_client: RedisClientAsync, lock_name: str, max_locks: int, token: str
) -> tuple[int | None, int | None]:
    """Return ``(held, candidate)`` for this acquire from one ``MGET``.

    ``held`` is the slot already holding *token*: a ``SET NX`` can land in
    Redis while its reply is lost (timeout, dropped connection), and the retry
    must reuse that slot rather than take a second one that stays held,
    unreleased, until its TTL.

    ``candidate`` is the first free slot walking from a start point fixed by
    *token* (``crc32``, stable across processes, unlike ``hash``). Every
    attempt of one acquire therefore picks the same slot while the free set is
    unchanged, so a retry racing its own still-in-flight earlier ``SET`` sends
    ``SET NX`` to the same key and only one of the two can land. ``None`` when
    every slot is taken.
    """
    first = zlib.crc32(token.encode()) % max_locks
    client = redis_client.redis_client
    if client is None:
        # Not connected: let ``_acquire_lock`` raise its typed connection error.
        return None, first
    values = await client.mget([f"{lock_name}:{slot}" for slot in range(max_locks)])
    decoded = [v.decode() if isinstance(v, bytes) else v for v in values]
    for slot, value in enumerate(decoded):
        if value == token:
            return slot, None
    for offset in range(max_locks):
        slot = (first + offset) % max_locks
        if decoded[slot] is None:
            return None, slot
    return None, None


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

    Idempotent across Temporal retries: the slot's value is a per-acquire
    token (see ``_acquire_token``), a retry that finds a slot already holding
    its token returns that slot instead of taking another, and the slot to
    try is chosen from the token, not at random (see ``_scan_slots``).

    Returns:
        dict[str, Any]: Lock information with the following keys:
            - slot_id (int): Allocated lock slot.
            - resource_id (str): Fully qualified lock resource ID.
            - owner_id (str): The value stored in the slot — the per-acquire
              token. Pass it to ``release_distributed_lock``.

    Raises:
        AppError: If lock acquisition fails due to Redis errors or invalid parameters.
    """
    # Input validation
    if max_locks <= 0:
        raise MaxLocksInvalidError(
            message=f"max_locks must be greater than 0, got {max_locks}",
            value_summary=str(max_locks),
        )
    token = _acquire_token(owner_id)
    resource_id = lock_name

    try:
        async with RedisClientAsync() as redis_client:
            held, slot = await _scan_slots(redis_client, lock_name, max_locks, token)
            if held is not None:
                resource_id = f"{lock_name}:{held}"
                logger.info(
                    "Lock already held by this acquire: slot=%s resource_id=%s",
                    held,
                    resource_id,
                )
                return {
                    "status": True,
                    "slot_id": held,
                    "resource_id": resource_id,
                    "owner_id": token,
                }

            if slot is None:
                # Every slot is taken; Temporal's retry policy tries again.
                raise LockAcquisitionError()
            resource_id = f"{lock_name}:{slot}"

            # Acquire lock - connection will stay open until context exits
            acquired = await redis_client._acquire_lock(resource_id, token, ttl_seconds)
            if acquired:
                logger.info("Lock acquired: slot=%s resource_id=%s", slot, resource_id)
                return {
                    "status": True,
                    "slot_id": slot,
                    "resource_id": resource_id,
                    "owner_id": token,
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
