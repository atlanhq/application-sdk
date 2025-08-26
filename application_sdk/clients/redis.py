"""Redis client for distributed locking with high availability support."""

from typing import Optional

import redis
from redis.sentinel import Sentinel

from application_sdk.common.error_codes import ClientError
from application_sdk.constants import (
    FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE,
    REDIS_CONNECTION_POOL_SIZE,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    REDIS_SENTINEL_HOSTS,
    REDIS_SENTINEL_SERVICE_NAME,
    REDIS_SOCKET_TIMEOUT,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class RedisClient:
    """High-availability Redis client for distributed operations.

    This client provides low-level Redis operations for distributed locking.
    Lock orchestration is handled by dedicated activities in lock_management.py
    to avoid Temporal workflow deadlock detection issues.
    """

    def __init__(self):
        self.redis_client = None
        self.connected = False

    def connect(self) -> None:
        """Establish connection to Redis only if strict locking is enabled."""

        if not FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE:
            logger.info("Strict locking disabled - skipping Redis connection")
            self.connected = False
            return

        # Only try to connect if strict locking is enabled
        try:
            if REDIS_SENTINEL_HOSTS:
                self._connect_via_sentinel()
            elif REDIS_HOST and REDIS_PORT:
                self._connect_standalone()
            else:
                raise ClientError.REQUEST_VALIDATION_ERROR

            self.redis_client.ping()  # type: ignore
            self.connected = True
            logger.info("Redis connection established for strict locking")

        except Exception as e:
            logger.error(f"Redis connection failed with strict locking enabled: {e}")
            # In strict mode, Redis failure is critical
            raise ClientError.CLIENT_AUTH_ERROR

    def _connect_via_sentinel(self) -> None:
        """Connect to Redis via Sentinel for high availability."""
        try:
            sentinel_hosts = [
                (host.strip(), int(port))
                for host_port in REDIS_SENTINEL_HOSTS.split(",")
                for host, port in [host_port.strip().rsplit(":", 1)]
            ]
        except ValueError as e:
            logger.error(f"Invalid Sentinel host format in REDIS_SENTINEL_HOSTS: {e}")
            raise ClientError.INPUT_VALIDATION_ERROR

        if not sentinel_hosts:
            logger.error("No Sentinel hosts configured")
            raise ClientError.REQUEST_VALIDATION_ERROR

        logger.info(f"Connecting to Redis via Sentinel: {sentinel_hosts}")
        logger.info(f"Service name: {REDIS_SENTINEL_SERVICE_NAME}")
        try:
            # Add sentinel-specific configuration
            sentinel = Sentinel(
                sentinel_hosts,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
                sentinel_kwargs={
                    "password": REDIS_PASSWORD if REDIS_PASSWORD else None,
                    "socket_connect_timeout": REDIS_SOCKET_TIMEOUT,
                },
            )

            self.redis_client = sentinel.master_for(
                REDIS_SENTINEL_SERVICE_NAME,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                max_connections=REDIS_CONNECTION_POOL_SIZE,
                retry_on_timeout=True,
                health_check_interval=30,
            )

        except Exception as e:
            logger.error(f"Redis connection failed with strict locking enabled: {e}")
            raise ClientError.CLIENT_AUTH_ERROR

    def _connect_standalone(self) -> None:
        """Connect to standalone Redis instance."""
        logger.debug(f"Connecting to standalone Redis: {REDIS_HOST}:{REDIS_PORT}")
        try:
            self.redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
                max_connections=REDIS_CONNECTION_POOL_SIZE,
            )
        except Exception as e:
            logger.error(f"Redis connection failed with strict locking enabled: {e}")
            raise ClientError.CLIENT_AUTH_ERROR

    def _acquire_lock(self, resource_id: str, owner_id: str, ttl_seconds: int) -> bool:
        """Atomically acquire a distributed lock.

        Note: This is a low-level method used by lock management activities.
        Use acquire_distributed_lock activity for workflow orchestration.
        """
        if not self.connected or not self.redis_client:
            return False

        try:
            result = self.redis_client.set(
                resource_id, owner_id, nx=True, ex=ttl_seconds
            )
            return bool(result)
        except Exception:
            return False

    def _release_lock(self, resource_id: str, owner_id: str) -> tuple[bool, str]:
        """Safely release a lock with ownership verification.

        Note: This is a low-level method used by lock management activities.
        Use release_distributed_lock activity for workflow orchestration.
        """
        if not self.connected or not self.redis_client:
            return False, "not_connected"

        try:
            release_script = """
            local current_owner = redis.call("GET", KEYS[1])
            if current_owner == false then
                return -1  -- Key doesn't exist
            elseif current_owner ~= ARGV[1] then
                return -2  -- Wrong owner
            else
                return redis.call("DEL", KEYS[1])  -- Success (returns 1)
            end
            """
            result = self.redis_client.eval(release_script, 1, resource_id, owner_id)
            if not isinstance(result, int):
                logger.warning(
                    f"Unexpected eval result type: {type(result)}, value: {result}"
                )
                return False, "unexpected_result_type"

            if result >= 1:
                return True, "success"
            elif result == -1:
                return True, "already_released"  # Not an error - TTL expired
            elif result == -2:
                return False, "wrong_owner"
            else:
                return False, "unknown_error"

        except Exception as e:
            logger.warning(f"Lock release failed: {e}")
            return False, "redis_error"

    def health_check(self) -> bool:
        """Check if Redis connection is healthy."""
        if not self.connected or not self.redis_client:
            return False

        try:
            self.redis_client.ping()
            return True
        except Exception as e:
            logger.warning(f"Redis health check failed: {e}")
            self.connected = False
            return False

    def disconnect(self) -> None:
        """Close connection to Redis cluster."""
        if self.redis_client:
            try:
                self.redis_client.close()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")
            finally:
                self.redis_client = None
                self.connected = False


# Global Redis client instance
_redis_client: Optional[RedisClient] = None


def get_redis_client() -> RedisClient:
    """Get the global Redis client instance."""
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
    return _redis_client
