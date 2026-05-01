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

from application_sdk.clients._interface import ClientInterface as ClientInterface
from application_sdk.clients.azure.auth import AzureAuthProvider as AzureAuthProvider
from application_sdk.clients.azure.client import AzureClient as AzureClient
from application_sdk.clients.base import BaseClient as BaseClient
from application_sdk.clients.models import DatabaseConfig as DatabaseConfig
from application_sdk.clients.redis import RedisClient as RedisClient
from application_sdk.clients.redis import RedisClientAsync as RedisClientAsync
from application_sdk.clients.sql import AsyncBaseSQLClient as AsyncBaseSQLClient
from application_sdk.clients.sql import BaseSQLClient as BaseSQLClient
from application_sdk.clients.ssl_utils import (
    create_ssl_context_with_custom_certs as create_ssl_context_with_custom_certs,
)
from application_sdk.clients.ssl_utils import get_ssl_context as get_ssl_context

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
