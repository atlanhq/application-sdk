from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, Awaitable, Callable, Optional

from temporalio import activity

from application_sdk.clients.redis import RedisClientAsync
from application_sdk.constants import (
    APPLICATION_NAME,
    IS_LOCKING_DISABLED,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def lock_per_run(
    lock_name: Optional[str] = None, ttl_seconds: int = 10
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Serialize an async method within an activity per workflow run.

    Uses Redis SET NX EX for acquisition and an owner-verified release.
    The lock key is namespaced and scoped to the current workflow run:
    ``{APPLICATION_NAME}:meth:{method_name}:run:{workflow_run_id}``.

    Args:
        lock_name: Optional explicit lock name. Defaults to the wrapped method's name.
        ttl_seconds: Lock TTL in seconds. Should cover worst-case wait + execution time.

    Returns:
        A decorator for async callables to guard them with a per-run distributed lock.
    """

    def _decorate(
        fn: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        @wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            if IS_LOCKING_DISABLED:
                return await fn(*args, **kwargs)

            run_id = activity.info().workflow_run_id
            name = lock_name or fn.__name__

            resource_id = f"{APPLICATION_NAME}:meth:{name}:run:{run_id}"
            owner_id = f"{APPLICATION_NAME}:{run_id}"

            async with RedisClientAsync() as rc:
                # Acquire with retry
                retry_count = 0
                while True:
                    logger.debug(f"Attempting to acquire lock: {resource_id}, owner: {owner_id}")
                    acquired = await rc._acquire_lock(
                        resource_id, owner_id, ttl_seconds
                    )
                    if acquired:
                        logger.info(f"Lock acquired: {resource_id}, owner: {owner_id}")
                        break
                    retry_count += 1
                    logger.debug(
                        f"Lock not available, retrying (attempt {retry_count}): {resource_id}"
                    )
                    await asyncio.sleep(5)

                try:
                    return await fn(*args, **kwargs)
                finally:
                    # Best-effort release; TTL guarantees cleanup if this fails
                    try:
                        logger.debug(f"Releasing lock: {resource_id}, owner: {owner_id}")
                        released, result = await rc._release_lock(resource_id, owner_id)
                        if released:
                            logger.info(f"Lock released successfully: {resource_id}")
                        else:
                            logger.warning(
                                f"Lock release failed (may already be released): {resource_id}, result: {result}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Exception during lock release for {resource_id}: {e}. TTL will handle cleanup."
                        )

        return _wrapped

    return _decorate


