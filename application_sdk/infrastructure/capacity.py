"""Distributed capacity management for API rate limiting.

.. deprecated:: 3.56
    Every name in this module is deprecated and will be removed in v4.0.0.
    The framework never configured a pool at startup and nothing in the SDK
    reads one, so ``get_capacity_pool()`` returned ``None`` unless the app
    called ``configure_capacity_pool()`` itself. Bound concurrency inside the
    app instead (for example with an ``asyncio.Semaphore``).

Provides a protocol for capacity pools that control how many concurrent
operations can run across multiple workers. When Redis is available,
permits are coordinated globally. Without Redis, falls back to local-only
behavior where each worker gets its full requested amount.

Global configuration:
    configure_capacity_pool() — call at worker startup
    get_capacity_pool() — retrieve the configured pool (or None)
"""

from __future__ import annotations

from typing import Protocol

from typing_extensions import deprecated

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@deprecated(
    "CapacityPool is deprecated and "
    "will be removed in v4.0.0. Nothing in the SDK configures or reads a "
    "capacity pool; bound concurrency inside the app (for example an "
    "asyncio.Semaphore) instead."
)
class CapacityPool(Protocol):
    """Deprecated: protocol for distributed capacity management — removed in v4.0.0 (never wired by the framework).

    Implementations control how many concurrent operations (e.g., API calls)
    can run across all workers sharing the same pool.
    """

    async def acquire(
        self,
        pool_name: str,
        requested: int,
        *,
        min_useful: int = 1,
        holder_id: str = "",
        ttl_seconds: int = 120,
    ) -> int:
        """Acquire permits from a capacity pool.

        Args:
            pool_name: Name of the capacity pool (e.g., "atlan-bulk-api").
            requested: Number of permits requested.
            min_useful: Minimum permits to be useful. If fewer are available,
                returns 0 (all-or-nothing below this threshold).
            holder_id: Unique identifier for this holder (for release/renew).
            ttl_seconds: Time-to-live for the lease. Permits are automatically
                reclaimed if not renewed within this window.

        Returns:
            Number of permits granted (0 if insufficient capacity).
        """
        ...

    async def release(self, pool_name: str, holder_id: str) -> None:
        """Release permits back to the pool.

        Args:
            pool_name: Name of the capacity pool.
            holder_id: Identifier of the holder releasing permits.
        """
        ...

    async def renew(
        self, pool_name: str, holder_id: str, ttl_seconds: int = 120
    ) -> bool:
        """Renew the TTL on held permits (heartbeat).

        Args:
            pool_name: Name of the capacity pool.
            holder_id: Identifier of the holder.
            ttl_seconds: New TTL in seconds.

        Returns:
            True if renewed successfully, False if holder not found.
        """
        ...


@deprecated(
    "LocalCapacityPool is deprecated and "
    "will be removed in v4.0.0. Nothing in the SDK configures or reads a "
    "capacity pool; bound concurrency inside the app (for example an "
    "asyncio.Semaphore) instead."
)
class LocalCapacityPool:
    """Deprecated: local-only capacity pool (no coordination) — removed in v4.0.0 (never wired by the framework).

    Always grants the full requested amount. Used when Redis is not
    configured (local development, single-worker deployments).
    """

    async def acquire(
        self,
        pool_name: str,
        requested: int,
        *,
        min_useful: int = 1,  # noqa: ARG002
        holder_id: str = "",  # noqa: ARG002
        ttl_seconds: int = 120,  # noqa: ARG002
    ) -> int:
        """Always grants the full requested amount."""
        logger.debug(
            "capacity acquired (local) pool={} requested={} granted={}",
            pool_name,
            requested,
            requested,
        )
        return requested

    async def release(self, pool_name: str, holder_id: str) -> None:
        """No-op for local pool."""
        logger.debug(
            "capacity released (local) pool={} holder={}", pool_name, holder_id
        )

    async def renew(
        self, pool_name: str, holder_id: str, ttl_seconds: int = 120
    ) -> bool:  # noqa: ARG002
        """No-op for local pool, always returns True."""
        return True


# ---------------------------------------------------------------------------
# Global capacity pool singleton
# ---------------------------------------------------------------------------
_capacity_pool: CapacityPool | None = None


@deprecated(
    "configure_capacity_pool is deprecated and "
    "will be removed in v4.0.0. Nothing in the SDK configures or reads a "
    "capacity pool; bound concurrency inside the app (for example an "
    "asyncio.Semaphore) instead."
)
def configure_capacity_pool(pool: CapacityPool) -> None:
    """Deprecated: configure the global capacity pool — removed in v4.0.0 (never wired by the framework).

    Call this at worker startup to configure the capacity pool used
    by activities that need to coordinate concurrent API access.

    Args:
        pool: CapacityPool implementation (Redis-backed or local fallback).

    Example::

        from application_sdk.infrastructure.capacity import (
            LocalCapacityPool, configure_capacity_pool,
        )

        configure_capacity_pool(LocalCapacityPool())
    """
    global _capacity_pool
    _capacity_pool = pool
    logger.debug("capacity pool configured pool_type={}", type(pool).__name__)


@deprecated(
    "get_capacity_pool is deprecated and "
    "will be removed in v4.0.0. Nothing in the SDK configures or reads a "
    "capacity pool; bound concurrency inside the app (for example an "
    "asyncio.Semaphore) instead."
)
def get_capacity_pool() -> CapacityPool | None:
    """Deprecated: get the configured capacity pool — removed in v4.0.0 (never wired by the framework).

    Returns:
        The configured CapacityPool, or None if not configured.
    """
    return _capacity_pool
