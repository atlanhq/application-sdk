"""Base client interfaces for Atlan applications.

Public API::

    from application_sdk.clients import (
        ClientInterface, BaseClient,
        BaseSQLClient, AsyncBaseSQLClient, DatabaseConfig,
        RedisClient, RedisClientAsync,
        AzureClient, AzureAuthProvider,
        get_ssl_context, create_ssl_context_with_custom_certs,
    )
"""

import importlib
from typing import TYPE_CHECKING

from application_sdk.clients._interface import ClientInterface as ClientInterface
from application_sdk.clients.base import BaseClient as BaseClient
from application_sdk.clients.models import DatabaseConfig as DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient as AsyncBaseSQLClient
from application_sdk.clients.sql import BaseSQLClient as BaseSQLClient
from application_sdk.clients.ssl_utils import (
    create_ssl_context_with_custom_certs as create_ssl_context_with_custom_certs,
)
from application_sdk.clients.ssl_utils import get_ssl_context as get_ssl_context

if TYPE_CHECKING:
    from application_sdk.clients.azure.auth import AzureAuthProvider
    from application_sdk.clients.azure.client import AzureClient
    from application_sdk.clients.redis import RedisClient, RedisClientAsync

# Symbols backed by optional extras ([azure] and [distributed_lock]) — lazy-loaded
# so that `from application_sdk.clients import BaseSQLClient` never pulls azure-* or redis.
_LAZY: dict[str, tuple[str, str]] = {
    "AzureAuthProvider": ("application_sdk.clients.azure.auth", "AzureAuthProvider"),
    "AzureClient": ("application_sdk.clients.azure.client", "AzureClient"),
    "RedisClient": ("application_sdk.clients.redis", "RedisClient"),
    "RedisClientAsync": ("application_sdk.clients.redis", "RedisClientAsync"),
}

__all__ = [
    "AsyncBaseSQLClient",
    "AzureAuthProvider",
    "AzureClient",
    "BaseClient",
    "BaseSQLClient",
    "ClientInterface",
    "DatabaseConfig",
    "RedisClient",
    "RedisClientAsync",
    "create_ssl_context_with_custom_certs",
    "get_ssl_context",
]


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(
            f"module 'application_sdk.clients' has no attribute {name!r}"
        )
    mod_name, attr = target
    return getattr(importlib.import_module(mod_name), attr)
