"""Redis-backed distributed capacity pool.

Uses Lua scripts for atomic acquire/release/renew operations. Each holder
gets a lease with a TTL — if the holder crashes or fails to renew, permits
are automatically reclaimed when the TTL expires.

Redis data model:
- capacity:{pool}:holders — HASH mapping holder_id -> granted count
- capacity:{pool}:ttl:{holder_id} — STRING with TTL (lease marker)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = get_logger(__name__)


# Lua script: Atomic acquire
# Keys: [holders_key]
# Args: [max_permits, requested, min_useful, holder_id, ttl_seconds, ttl_key_prefix]
#
# 1. Scan holders hash for all current holders
# 2. For each holder, check if TTL key still exists
# 3. Remove expired holders (reclaim permits)
# 4. Calculate available = max_permits - sum(active holders)
# 5. Grant min(requested, available) if >= min_useful, else 0
# 6. Set holder's grant in hash + create TTL key with SETEX
_ACQUIRE_SCRIPT = """
local holders_key = KEYS[1]
local max_permits = tonumber(ARGV[1])
local requested = tonumber(ARGV[2])
local min_useful = tonumber(ARGV[3])
local holder_id = ARGV[4]
local ttl_seconds = tonumber(ARGV[5])
local ttl_prefix = ARGV[6]

-- Scan all holders and clean up expired ones
local holders = redis.call('HGETALL', holders_key)
local active_total = 0
local expired = {}

for i = 1, #holders, 2 do
    local hid = holders[i]
    local count = tonumber(holders[i + 1])
    local ttl_key = ttl_prefix .. hid
    if redis.call('EXISTS', ttl_key) == 1 then
        active_total = active_total + count
    else
        table.insert(expired, hid)
    end
end

-- Remove expired holders
for _, hid in ipairs(expired) do
    redis.call('HDEL', holders_key, hid)
end

-- Calculate available capacity
local available = max_permits - active_total
local granted = math.min(requested, available)

if granted < min_useful then
    return 0
end

-- Grant permits
redis.call('HSET', holders_key, holder_id, granted)
local ttl_key = ttl_prefix .. holder_id
redis.call('SETEX', ttl_key, ttl_seconds, '1')

return granted
"""

# Lua script: Atomic release
# Keys: [holders_key]
# Args: [holder_id, ttl_key]
_RELEASE_SCRIPT = """
local holders_key = KEYS[1]
local holder_id = ARGV[1]
local ttl_key = ARGV[2]

redis.call('HDEL', holders_key, holder_id)
redis.call('DEL', ttl_key)
return 1
"""

# Lua script: Atomic renew
# Keys: [holders_key]
# Args: [holder_id, ttl_seconds, ttl_key]
_RENEW_SCRIPT = """
local holders_key = KEYS[1]
local holder_id = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local ttl_key = ARGV[3]

if redis.call('HEXISTS', holders_key, holder_id) == 1 then
    redis.call('SETEX', ttl_key, ttl_seconds, '1')
    return 1
end
return 0
"""


class RedisCapacityPool:
    """Distributed capacity pool backed by Redis.

    Uses Lua scripts for atomic operations to ensure correctness
    under concurrent access from multiple workers.

    Args:
        redis_client: Async Redis client instance.
        max_permits: Maximum total permits across all holders.
    """

    def __init__(self, redis_client: "Redis", max_permits: int = 20) -> None:
        self._redis = redis_client
        self._max_permits = max_permits

    def _holders_key(self, pool_name: str) -> str:
        return f"capacity:{pool_name}:holders"

    def _ttl_prefix(self, pool_name: str) -> str:
        return f"capacity:{pool_name}:ttl:"

    def _ttl_key(self, pool_name: str, holder_id: str) -> str:
        return f"capacity:{pool_name}:ttl:{holder_id}"

    async def acquire(
        self,
        pool_name: str,
        requested: int,
        *,
        min_useful: int = 1,
        holder_id: str = "",
        ttl_seconds: int = 120,
    ) -> int:
        """Acquire permits from the distributed pool.

        Uses a Lua script for atomicity. Expired holders are cleaned up
        during the acquire operation.
        """
        holders_key = self._holders_key(pool_name)
        ttl_prefix = self._ttl_prefix(pool_name)

        result = await self._redis.eval(  # type: ignore[misc]  # redis async stubs return union
            _ACQUIRE_SCRIPT,
            1,
            holders_key,
            str(self._max_permits),
            str(requested),
            str(min_useful),
            holder_id,
            str(ttl_seconds),
            ttl_prefix,
        )
        granted = int(result)

        if granted > 0:
            logger.info(
                "Acquired capacity permits",
                pool=pool_name,
                holder=holder_id,
                requested=requested,
                granted=granted,
                max_permits=self._max_permits,
            )
        else:
            logger.warning(
                "No capacity permits available",
                pool=pool_name,
                holder=holder_id,
                requested=requested,
                min_useful=min_useful,
                max_permits=self._max_permits,
            )

        return granted

    async def release(self, pool_name: str, holder_id: str) -> None:
        """Release permits back to the pool."""
        holders_key = self._holders_key(pool_name)
        ttl_key = self._ttl_key(pool_name, holder_id)

        await self._redis.eval(  # type: ignore[misc]  # redis async stubs return union
            _RELEASE_SCRIPT,
            1,
            holders_key,
            holder_id,
            ttl_key,
        )

        logger.info(
            "Released capacity permits",
            pool=pool_name,
            holder=holder_id,
        )

    async def renew(
        self, pool_name: str, holder_id: str, ttl_seconds: int = 120
    ) -> bool:
        """Renew the TTL on held permits."""
        holders_key = self._holders_key(pool_name)
        ttl_key = self._ttl_key(pool_name, holder_id)

        result = await self._redis.eval(  # type: ignore[misc]  # redis async stubs return union
            _RENEW_SCRIPT,
            1,
            holders_key,
            holder_id,
            str(ttl_seconds),
            ttl_key,
        )
        renewed = bool(int(result))

        if not renewed:
            logger.warning(
                "Failed to renew capacity permits (holder not found)",
                pool=pool_name,
                holder=holder_id,
            )

        return renewed
