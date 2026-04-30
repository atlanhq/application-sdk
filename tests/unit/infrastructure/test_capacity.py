"""Unit tests for distributed capacity management."""

from __future__ import annotations

import pytest

from application_sdk.infrastructure.capacity import (
    LocalCapacityPool,
    configure_capacity_pool,
    get_capacity_pool,
)


class TestLocalCapacityPool:
    """Tests for LocalCapacityPool."""

    def setup_method(self) -> None:
        self.pool = LocalCapacityPool()

    @pytest.mark.asyncio
    async def test_acquire_returns_requested_amount(self) -> None:
        granted = await self.pool.acquire("test-pool", requested=10)
        assert granted == 10

    @pytest.mark.asyncio
    async def test_acquire_any_amount(self) -> None:
        for amount in [1, 5, 100, 1000]:
            granted = await self.pool.acquire("test-pool", requested=amount)
            assert granted == amount

    @pytest.mark.asyncio
    async def test_acquire_with_min_useful_still_returns_full(self) -> None:
        # LocalCapacityPool ignores min_useful — always returns requested
        granted = await self.pool.acquire("test-pool", requested=10, min_useful=8)
        assert granted == 10

    @pytest.mark.asyncio
    async def test_acquire_with_holder_id(self) -> None:
        granted = await self.pool.acquire(
            "test-pool", requested=5, holder_id="worker-1"
        )
        assert granted == 5

    @pytest.mark.asyncio
    async def test_acquire_with_ttl(self) -> None:
        granted = await self.pool.acquire("test-pool", requested=5, ttl_seconds=60)
        assert granted == 5

    @pytest.mark.asyncio
    async def test_release_is_noop(self) -> None:
        # Should not raise
        await self.pool.release("test-pool", holder_id="worker-1")

    @pytest.mark.asyncio
    async def test_renew_always_returns_true(self) -> None:
        result = await self.pool.renew("test-pool", holder_id="worker-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_renew_with_ttl(self) -> None:
        result = await self.pool.renew(
            "test-pool", holder_id="worker-1", ttl_seconds=60
        )
        assert result is True


class TestCapacityPoolProtocolCompliance:
    """Tests that LocalCapacityPool satisfies CapacityPool protocol."""

    def test_local_capacity_pool_has_acquire(self) -> None:
        pool = LocalCapacityPool()
        assert hasattr(pool, "acquire")
        assert callable(pool.acquire)

    def test_local_capacity_pool_has_release(self) -> None:
        pool = LocalCapacityPool()
        assert hasattr(pool, "release")
        assert callable(pool.release)

    def test_local_capacity_pool_has_renew(self) -> None:
        pool = LocalCapacityPool()
        assert hasattr(pool, "renew")
        assert callable(pool.renew)


class TestCapacityPoolSingleton:
    """Tests for configure_capacity_pool and get_capacity_pool."""

    def teardown_method(self) -> None:
        # Reset global singleton
        from application_sdk.infrastructure import capacity as cap_module

        cap_module._capacity_pool = None

    def test_get_capacity_pool_returns_none_when_not_configured(self) -> None:
        from application_sdk.infrastructure import capacity as cap_module

        cap_module._capacity_pool = None
        assert get_capacity_pool() is None

    def test_configure_and_get_capacity_pool(self) -> None:
        pool = LocalCapacityPool()
        configure_capacity_pool(pool)
        result = get_capacity_pool()
        assert result is pool

    def test_configure_replaces_existing_pool(self) -> None:
        pool1 = LocalCapacityPool()
        pool2 = LocalCapacityPool()
        configure_capacity_pool(pool1)
        configure_capacity_pool(pool2)
        assert get_capacity_pool() is pool2
