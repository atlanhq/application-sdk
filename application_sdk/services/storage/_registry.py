"""Store instance registry — maps store_name strings to obstore instances.

Lazily creates and caches obstore store instances. Thread-safe via asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from application_sdk.services.storage._config import (
    StorageBindingConfig,
    create_store,
    resolve_store_config,
)

logger = logging.getLogger(__name__)


class StoreRegistry:
    """Registry mapping store_name strings to lazily-created obstore instances."""

    _stores: dict[str, Any] = {}
    _configs: dict[str, StorageBindingConfig] = {}
    _lock: asyncio.Lock | None = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Get or create the async lock (must be created in an event loop context)."""
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def get_store(cls, store_name: str) -> Any:
        """Get or create an obstore instance for the given store_name.

        Uses double-check locking for thread-safe lazy initialization.
        """
        if store_name in cls._stores:
            return cls._stores[store_name]

        async with cls._get_lock():
            # Double-check after acquiring lock
            if store_name in cls._stores:
                return cls._stores[store_name]

            config = resolve_store_config(store_name)
            store = create_store(config)
            cls._stores[store_name] = store
            cls._configs[store_name] = config
            logger.info(
                "Created %s store for binding '%s' (bucket=%s)",
                config.provider,
                store_name,
                config.bucket,
            )
            return store

    @classmethod
    async def get_bucket(cls, store_name: str) -> str:
        """Get the bucket name for a store_name. Ensures store is initialized."""
        if store_name not in cls._configs:
            await cls.get_store(store_name)
        return cls._configs[store_name].bucket

    @classmethod
    def reset(cls) -> None:
        """Clear all cached stores. Used for testing."""
        cls._stores.clear()
        cls._configs.clear()
        cls._lock = None
