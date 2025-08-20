"""Redis client for distributed locking with high availability support."""

from typing import Optional

import redis
from redis.sentinel import Sentinel

from application_sdk.constants import (
    REDIS_CONNECTION_POOL_SIZE,
    REDIS_DB,
    REDIS_PASSWORD,
    REDIS_SENTINEL_HOSTS,
    REDIS_SENTINEL_SERVICE_NAME,
    REDIS_SOCKET_TIMEOUT,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class DistributedLock:
    """Context manager for distributed locks with automatic cleanup."""

    def __init__(
        self, client: "RedisClient", resource_id: str, owner_id: str, ttl: int
    ):
        self.client = client
        self.resource_id = resource_id
        self.owner_id = owner_id
        self.ttl = ttl
        self.acquired = False

    def __enter__(self) -> bool:
        """Attempt to acquire the distributed lock."""
        self.acquired = self.client._acquire_lock(
            self.resource_id, self.owner_id, self.ttl
        )
        return self.acquired

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release the lock if it was acquired."""
        if self.acquired:
            self.client._release_lock(self.resource_id, self.owner_id)


class RedisClient:
    """High-availability Redis client for distributed operations."""

    def __init__(self):
        self.redis_client = None
        self.connected = False

    def connect(self) -> None:
        """Establish connection to Redis (Sentinel or standalone mode).

        Gracefully handles connection failures to allow app to continue
        functioning without distributed locking capabilities.
        """
        try:
            if REDIS_SENTINEL_HOSTS:
                self._connect_via_sentinel()
            else:
                self._connect_standalone()

            self.redis_client.ping()  # type: ignore
            self.connected = True
            logger.info("Redis connection established successfully")

        except Exception as e:
            logger.warning(f"Redis connection failed: {e}")
            logger.warning("Application will continue without distributed locking")
            self.connected = False

    def _connect_via_sentinel(self) -> None:
        """Connect to Redis via Sentinel for high availability."""
        try:
            sentinel_hosts = [
                (host.strip(), int(port))
                for host_port in REDIS_SENTINEL_HOSTS.split(",")
                for host, port in [host_port.strip().rsplit(":", 1)]
            ]
        except ValueError as e:
            raise ValueError(
                f"Invalid Sentinel host format in REDIS_SENTINEL_HOSTS: {e}"
            )

        if not sentinel_hosts:
            raise ValueError("No Sentinel hosts configured")

        logger.info(f"Connecting to Redis via Sentinel: {sentinel_hosts}")
        logger.info(f"Service name: {REDIS_SENTINEL_SERVICE_NAME}")

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

    def _connect_standalone(self) -> None:
        """Connect to standalone Redis instance."""
        from application_sdk.constants import REDIS_HOST, REDIS_PORT

        logger.info(f"Connecting to standalone Redis: {REDIS_HOST}:{REDIS_PORT}")

        self.redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            max_connections=REDIS_CONNECTION_POOL_SIZE,
        )

    def lock(
        self, resource_id: str, owner_id: str, ttl_seconds: int = 300
    ) -> "DistributedLock":
        """Create a distributed lock with automatic cleanup.

        Usage:
            with redis_client.lock("resource_name", "owner_id", 60) as acquired:
                if acquired:
                    # Critical section - lock is held
                    perform_exclusive_operation()
                # Lock is automatically released here
        """
        return DistributedLock(self, resource_id, owner_id, ttl_seconds)

    def _acquire_lock(self, resource_id: str, owner_id: str, ttl_seconds: int) -> bool:
        """Atomically acquire a distributed lock."""
        if not self.connected or not self.redis_client:
            return False

        try:
            result = self.redis_client.set(
                resource_id, owner_id, nx=True, ex=ttl_seconds
            )
            return bool(result)
        except Exception:
            return False

    def _release_lock(self, resource_id: str, owner_id: str) -> bool:
        """Safely release a lock with ownership verification."""
        if not self.connected or not self.redis_client:
            return False

        try:
            release_script = """
            local current_owner = redis.call("GET", KEYS[1])
            if current_owner == ARGV[1] then
                return redis.call("DEL", KEYS[1])
            else
                return 0
            end
            """
            result = self.redis_client.eval(release_script, 1, resource_id, owner_id)
            return bool(result)
        except Exception:
            return False

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
