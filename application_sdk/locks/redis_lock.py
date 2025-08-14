import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple

import aioredis
from aioredlock import Aioredlock

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LockResult:
    """Result of a lock acquisition attempt."""

    def __init__(self, success: bool):
        self.success = success


class RedisLockProvider:
    """Simple Redis-based distributed lock provider with Sentinel support."""

    def __init__(
        self,
        sentinel_hosts: List[Tuple[str, int]],
        service_name: str,
        password: Optional[str] = None,
    ):
        self.sentinel_hosts = sentinel_hosts
        self.service_name = service_name
        self.password = password
        self._lock_manager = None
        self._redis = None
        self._initialized = False

    async def initialize(self):
        """Initialize Redis connection."""
        if self._initialized:
            return

        try:
            sentinel = aioredis.Sentinel(self.sentinel_hosts, password=self.password)
            self._redis = sentinel.master_for(self.service_name)
            self._lock_manager = Aioredlock([self._redis])
            await self._redis.ping()
            self._initialized = True
            logger.info(
                f"Redis lock provider initialized with service: {self.service_name}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize Redis lock provider: {e}")
            raise

    @asynccontextmanager
    async def try_lock(self, resource_id: str, lock_owner: str, expiry_in_seconds: int):
        """Try to acquire a distributed lock."""
        if not self._initialized:
            await self.initialize()

        lock = None
        try:
            lock = await self._lock_manager.lock(
                resource_id, lock_timeout=expiry_in_seconds * 1000
            )
            yield LockResult(success=bool(lock))
        except Exception as e:
            logger.error(f"Error during lock operation: {e}")
            yield LockResult(success=False)
        finally:
            if lock:
                try:
                    await self._lock_manager.unlock(lock)
                except Exception as e:
                    logger.error(f"Error releasing lock: {e}")

    async def is_available(self):
        """Check if Redis is available."""
        try:
            if not self._redis:
                return False
            await self._redis.ping()
            return True
        except:
            return False
