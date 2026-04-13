"""Client package — all connector clients inherit from ``Client``.

Re-exports the core classes for convenient imports::

    from application_sdk.clients import Client
    from application_sdk.clients import SQLClient, AsyncSQLClient
    from application_sdk.clients import HTTPClient
"""

from application_sdk.clients.client import Client

# Backward-compat aliases
BaseClient = Client
ClientInterface = Client

__all__ = [
    "Client",
    "BaseClient",
    "ClientInterface",
]
