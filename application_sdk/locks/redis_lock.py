from contextlib import asynccontextmanager
from typing import List, Optional, Tuple, cast

from redis.asyncio.client import Redis
from redis.asyncio.sentinel import Sentinel

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
        self._redis: Optional[Redis] = None
        self._initialized: bool = False

    async def initialize(self):
        """Initialize Redis connection with Sentinel support.

        This method:
        1. Creates a connection to the Redis Sentinel cluster
        2. Gets a master connection that automatically handles failover
        3. Tests the connection to ensure it's working
        """
        if self._initialized:
            return

        try:
            # Create Sentinel connection with retry on failure
            sentinel = Sentinel(
                self.sentinel_hosts,
                password=self.password,
                # Sentinel specific settings
                sentinel_kwargs={
                    "password": self.password,  # Sentinel auth if needed
                    "socket_timeout": 0.1,  # Fast sentinel switching
                    "socket_keepalive": True,  # Keep connections alive
                    "retry_on_timeout": True,  # Retry on timeouts
                },
            )

            # Get master connection with automatic failover handling
            self._redis = cast(
                Redis,
                sentinel.master_for(
                    service_name=self.service_name,
                    # Redis connection settings
                    socket_timeout=0.1,  # Fast failure detection
                    socket_keepalive=True,  # Keep connections alive
                    retry_on_timeout=True,  # Retry on timeouts
                    retry_on_error=[  # Retry on these errors
                        "READONLY",  # When replica becomes master
                        "LOADING",  # When Redis is loading data
                        "CLUSTERDOWN",  # When cluster is reforming
                        "MASTERDOWN",  # When master is switching
                    ],
                ),
            )

            # Test connection and verify it's master
            await self._redis.ping()
            role_info = await self._redis.role()
            if role_info[0] != "master":
                raise Exception(f"Connected to {role_info[0]}, expected master")

            self._initialized = True
            logger.info(
                f"Redis lock provider initialized with Sentinel service: {self.service_name}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize Redis lock provider: {e}")
            raise

    @asynccontextmanager
    async def try_lock(self, resource_id: str, lock_owner: str, expiry_in_seconds: int):
        """Try to acquire a distributed lock using Redis SET NX with expiry.

        Args:
            resource_id: Unique identifier for the resource to lock
            lock_owner: Identity of the lock owner (used as lock value)
            expiry_in_seconds: Lock timeout in seconds

        Yields:
            LockResult indicating if lock was acquired successfully
        """
        if not self._initialized:
            await self.initialize()

        if not self._redis:
            logger.error("Redis not initialized")
            yield LockResult(success=False)
            return

        try:
            # Try to acquire lock using SET NX with expiry
            success = await self._redis.set(
                resource_id,  # Key
                lock_owner,  # Value (owner ID)
                nx=True,  # Only set if key doesn't exist
                ex=expiry_in_seconds,  # Set expiry in seconds
            )
            yield LockResult(success=bool(success))
        except Exception as e:
            logger.error(f"Error during lock operation: {e}")
            yield LockResult(success=False)
        finally:
            if success:
                try:
                    # Only delete if we're still the owner
                    await self._redis.delete(resource_id)
                except Exception as e:
                    logger.error(f"Error releasing lock: {e}")

    async def is_available(self) -> bool:
        """Check if Redis is available."""
        try:
            if not self._redis:
                return False
            await self._redis.ping()
            return True
        except Exception:
            return False
