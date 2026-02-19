"""Base client interfaces for Atlan applications.

This module provides the abstract base class for all client implementations,
defining the core interface that any client must implement for connecting
to external services and data sources.

.. deprecated:: 3.0.0
    The ClientInterface and client_class pattern is deprecated.
    Use the new credential system with ``declare_credentials()`` and
    ``ctx.http`` / ``ctx.credentials`` instead. See the credential
    migration guide for details.
"""

import warnings
from abc import ABC, abstractmethod
from typing import Any


class ClientInterface(ABC):
    """Base interface class for implementing client connections.

    This abstract class defines the required methods that any client implementation
    must provide for establishing and managing connections to data sources.

    .. deprecated:: 3.0.0
        Use the new credential system instead. Define credentials in your handler
        using ``declare_credentials()`` and access them via ``ctx.http`` for
        authenticated HTTP requests or ``ctx.credentials`` for raw credential values.

        Example migration::

            # Old pattern (deprecated)
            class MyClient(BaseClient):
                async def load(self, **kwargs):
                    self.http_headers = {"Authorization": f"Bearer {kwargs['token']}"}

            # New pattern (recommended)
            class MyHandler(HandlerInterface):
                @classmethod
                def declare_credentials(cls) -> List[Credential]:
                    return [Credential(name="api", auth=AuthMode.BEARER_TOKEN)]

                async def my_activity(self, ctx: CredentialContext):
                    response = await ctx.http.get("api", "/users")
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Warn when ClientInterface is subclassed."""
        super().__init_subclass__(**kwargs)
        warnings.warn(
            f"Subclassing ClientInterface ({cls.__name__}) is deprecated. "
            "Use the new credential system with declare_credentials() and "
            "ctx.http / ctx.credentials instead. See credential migration guide.",
            DeprecationWarning,
            stacklevel=2,
        )

    @abstractmethod
    async def load(self, *args: Any, **kwargs: Any) -> None:
        """Establish the client connection.

        This method should handle the initialization and connection setup
        for the specific client implementation.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("load method is not implemented")

    async def close(self, *args: Any, **kwargs: Any) -> None:
        """Close the client connection.

        This method should properly terminate the connection and clean up
        any resources used by the client. By default, it does nothing.
        Subclasses should override this method if cleanup is needed.
        """
        return
