"""Redis client for distributed locking with high availability support."""

from enum import Enum

import redis.asyncio as redis
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from application_sdk.common.error_codes import ClientError
from application_sdk.constants import (
    IS_LOCKING_DISABLED,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    REDIS_SENTINEL_HOSTS,
    REDIS_SENTINEL_SERVICE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LockReleaseResult(Enum):
    """Enum for lock release operation results."""

    SUCCESS = "success"
    ALREADY_RELEASED = "already_released"
    WRONG_OWNER = "wrong_owner"


_LOCK_RELEASE_LUA_SCRIPT = """
    local current_owner = redis.call("GET", KEYS[1])
    if current_owner == false then
        return -1  -- Key doesn't exist
    elseif current_owner ~= ARGV[1] then
        return -2  -- Wrong owner
    else
        return redis.call("DEL", KEYS[1])  -- Success (returns 1)
    end
"""


class RedisClient:
    """High-availability Redis client for distributed operations.

    This client provides low-level Redis operations for distributed locking.
    Lock orchestration is handled by dedicated activities in lock_management.py
    to avoid Temporal workflow deadlock detection issues.

    Implements proper resource management with context manager support.
    """

    def __init__(self):
        """Initialize Redis client configuration."""
        self.redis_client = None

        # Prepare connection configuration
        if IS_LOCKING_DISABLED:
            logger.info("Strict locking disabled - skipping Redis connection")
            return

        # Validate Redis configuration
        if not REDIS_PASSWORD or (
            not REDIS_SENTINEL_HOSTS and not (REDIS_HOST and REDIS_PORT)
        ):
            logger.error(
                "Redis configuration invalid: REDIS_PASSWORD is required and either REDIS_SENTINEL_HOSTS or REDIS_HOST/REDIS_PORT must be configured"
            )
            raise ClientError(
                f"{ClientError.REQUEST_VALIDATION_ERROR}: Redis configuration invalid - REDIS_PASSWORD is required and either REDIS_SENTINEL_HOSTS or REDIS_HOST/REDIS_PORT must be configured"
            )

    async def _connect(self):
        """Establish async Redis connection."""
        if IS_LOCKING_DISABLED:
            logger.info("Locking disabled - Redis client will operate in no-op mode")
            return

        try:
            if REDIS_SENTINEL_HOSTS:
                await self._connect_via_sentinel_async()
            else:
                await self._connect_standalone_async()

            # Test connection
            if not self.redis_client:
                raise ClientError(
                    f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection failed"
                )

            await self.redis_client.ping()
            logger.info("Async Redis connection established for strict locking")

        except ConnectionError as e:
            logger.error(f"Redis connection failed - server unreachable: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection failed - server unreachable"
            )
        except TimeoutError as e:
            logger.error(f"Redis connection timeout: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection timeout"
            )
        except RedisError as e:
            logger.error(f"Redis authentication or protocol error: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis authentication or protocol error"
            )
        except Exception as e:
            logger.error(f"Unexpected error during Redis connection: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected Redis connection error"
            )

    async def close(self) -> None:
        """Close the Redis client and clean up resources."""
        if self.redis_client:
            try:
                await self.redis_client.aclose()
                logger.info("Async Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing async Redis connection: {e}")
            finally:
                self.redis_client = None

    async def __aenter__(self):
        """Async context manager entry."""
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit with guaranteed cleanup."""
        await self.close()

    async def _connect_via_sentinel_async(self) -> None:
        """Connect to Redis via Sentinel using async client."""
        # Parse Sentinel hosts
        try:
            sentinel_hosts = [
                (host.strip(), int(port))
                for host_port in REDIS_SENTINEL_HOSTS.split(",")
                for host, port in [host_port.strip().rsplit(":", 1)]
            ]
        except ValueError as e:
            logger.error(
                f"Invalid Sentinel host format in REDIS_SENTINEL_HOSTS '{REDIS_SENTINEL_HOSTS}': {e}"
            )
            raise

        if not sentinel_hosts:
            logger.error("No Sentinel hosts configured")
            raise ClientError(
                f"{ClientError.REQUEST_VALIDATION_ERROR}: No Sentinel hosts configured"
            )

        logger.info(f"Connecting to Redis via async Sentinel: {sentinel_hosts}")
        logger.info(f"Service name: {REDIS_SENTINEL_SERVICE_NAME}")

        try:
            # Create Sentinel with password
            sentinel = redis.sentinel.Sentinel(
                sentinel_hosts, sentinel_kwargs={"password": REDIS_PASSWORD}
            )

            # Create master client with password
            self.redis_client = sentinel.master_for(
                REDIS_SENTINEL_SERVICE_NAME, password=REDIS_PASSWORD
            )

        except ConnectionError as e:
            logger.error(f"Redis Sentinel connection failed - servers unreachable: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis Sentinel connection failed - servers unreachable"
            )
        except TimeoutError as e:
            logger.error(f"Redis Sentinel connection timeout: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis Sentinel connection timeout"
            )
        except RedisError as e:
            logger.error(f"Redis Sentinel authentication or protocol error: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis Sentinel authentication or protocol error"
            )
        except Exception as e:
            logger.error(f"Unexpected error during Redis Sentinel connection: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected Redis Sentinel connection error"
            )

    async def _connect_standalone_async(self) -> None:
        """Connect to standalone Redis instance using async client."""
        logger.debug(f"Connecting to standalone async Redis: {REDIS_HOST}:{REDIS_PORT}")

        try:
            self.redis_client = redis.Redis(
                host=REDIS_HOST, port=int(REDIS_PORT), password=REDIS_PASSWORD
            )

        except ConnectionError as e:
            logger.error(
                f"Redis standalone connection failed - server unreachable at {REDIS_HOST}:{REDIS_PORT}: {e}"
            )
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis standalone connection failed - server unreachable"
            )
        except TimeoutError as e:
            logger.error(
                f"Redis standalone connection timeout to {REDIS_HOST}:{REDIS_PORT}: {e}"
            )
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis standalone connection timeout"
            )
        except RedisError as e:
            logger.error(f"Redis standalone authentication or protocol error: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis standalone authentication or protocol error"
            )
        except Exception as e:
            logger.error(f"Unexpected error during Redis standalone connection: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected Redis standalone connection error"
            )

    async def _acquire_lock(
        self, resource_id: str, owner_id: str = "default_owner", ttl_seconds: int = 100
    ) -> bool:
        """Atomically acquire a distributed lock.

        Note: This is a low-level method used by lock management activities.
        Use acquire_distributed_lock activity for workflow orchestration.

        Returns:
            True if lock was acquired, False if lock is already held by another owner

        Raises:
            ClientError: If Redis connection or operation fails
        """
        if not self.redis_client:
            logger.error("Redis client not initialized")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection failed"
            )

        try:
            result = await self.redis_client.set(
                resource_id, owner_id, nx=True, ex=ttl_seconds
            )
            return bool(result)
        except ConnectionError as e:
            logger.error(
                f"Redis connection lost during lock acquisition for {resource_id}: {e}"
            )
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection lost during lock acquisition"
            )
        except TimeoutError as e:
            logger.error(
                f"Redis timeout during lock acquisition for {resource_id}: {e}"
            )
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis timeout during lock acquisition"
            )
        except RedisError as e:
            logger.error(f"Redis error during lock acquisition for {resource_id}: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis error during lock acquisition"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error during lock acquisition for {resource_id}: {e}"
            )
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected error during lock acquisition"
            )

    async def _release_lock(
        self, resource_id: str, owner_id: str = "default_owner"
    ) -> tuple[bool, LockReleaseResult]:
        """Safely release a lock with ownership verification.

        Note: This is a low-level method used by lock management activities.
        Use release_distributed_lock activity for workflow orchestration.

        Returns:
            Tuple of (success: bool, result: LockReleaseResult)
            - (True, LockReleaseResult.SUCCESS): Lock released successfully
            - (True, LockReleaseResult.ALREADY_RELEASED): Lock was already released (TTL expired)
            - (False, LockReleaseResult.WRONG_OWNER): Lock owned by different owner

        Raises:
            ClientError: If Redis connection or operation fails
        """
        if not self.redis_client:
            logger.error("Redis client not initialized")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection failed"
            )

        try:
            result = await self.redis_client.eval(
                _LOCK_RELEASE_LUA_SCRIPT, 1, resource_id, owner_id
            )
            if not isinstance(result, int):
                logger.error(
                    f"Unexpected eval result type for {resource_id}: {type(result)}, value: {result}"
                )
                raise ClientError(
                    f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection failed"
                )

            if result >= 1:
                return True, LockReleaseResult.SUCCESS
            elif result == -1:
                return (
                    True,
                    LockReleaseResult.ALREADY_RELEASED,
                )  # Not an error - TTL expired
            elif result == -2:
                return False, LockReleaseResult.WRONG_OWNER
            else:
                logger.error(f"Unknown Redis eval result for {resource_id}: {result}")
                raise ClientError(
                    f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection failed"
                )

        except ConnectionError as e:
            logger.error(
                f"Redis connection lost during lock release for {resource_id}: {e}"
            )
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis connection lost during lock release"
            )
        except TimeoutError as e:
            logger.error(f"Redis timeout during lock release for {resource_id}: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis timeout during lock release"
            )
        except RedisError as e:
            logger.error(f"Redis error during lock release for {resource_id}: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Redis error during lock release"
            )
        except Exception as e:
            logger.error(f"Unexpected error during lock release for {resource_id}: {e}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected error during lock release"
            )
