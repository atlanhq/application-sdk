"""Unit tests for the deprecated Redis-backed capacity pool.

``RedisCapacityPool`` is deprecated (removal v4.0.0): the framework never
constructed one. These tests pin the deprecation and keep the Lua-script
calls honest until removal. Redis is a mock — the scripts themselves run
only against a real server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.infrastructure._redis import RedisCapacityPool
from application_sdk.infrastructure._redis.capacity import (
    _ACQUIRE_SCRIPT,
    _RELEASE_SCRIPT,
    _RENEW_SCRIPT,
)


def _redis(eval_result: object) -> MagicMock:
    client = MagicMock(name="redis")
    client.eval = AsyncMock(return_value=eval_result)
    return client


def test_construction_warns() -> None:
    with pytest.warns(DeprecationWarning, match="removed in v4.0.0"):
        RedisCapacityPool(_redis(0), max_permits=5)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
class TestRedisCapacityPool:
    @pytest.mark.asyncio
    async def test_acquire_passes_pool_keys_and_limits(self) -> None:
        client = _redis(3)
        pool = RedisCapacityPool(client, max_permits=5)

        granted = await pool.acquire(
            "api", 4, min_useful=2, holder_id="w1", ttl_seconds=30
        )

        assert granted == 3
        client.eval.assert_awaited_once_with(
            _ACQUIRE_SCRIPT,
            1,
            "capacity:api:holders",
            "5",
            "4",
            "2",
            "w1",
            "30",
            "capacity:api:ttl:",
        )

    @pytest.mark.asyncio
    async def test_acquire_returns_zero_when_exhausted(self) -> None:
        pool = RedisCapacityPool(_redis(0))
        assert await pool.acquire("api", 4, holder_id="w1") == 0

    @pytest.mark.asyncio
    async def test_release_targets_holder_ttl_key(self) -> None:
        client = _redis(1)
        await RedisCapacityPool(client).release("api", "w1")
        client.eval.assert_awaited_once_with(
            _RELEASE_SCRIPT, 1, "capacity:api:holders", "w1", "capacity:api:ttl:w1"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("result", "expected"), [(1, True), (0, False)])
    async def test_renew_reports_whether_holder_exists(
        self, result: int, expected: bool
    ) -> None:
        client = _redis(result)
        assert await RedisCapacityPool(client).renew("api", "w1", 45) is expected
        client.eval.assert_awaited_once_with(
            _RENEW_SCRIPT,
            1,
            "capacity:api:holders",
            "w1",
            "45",
            "capacity:api:ttl:w1",
        )
