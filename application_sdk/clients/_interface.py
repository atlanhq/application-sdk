from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Self

from application_sdk.clients.sql_errors import AbstractClientLoadError


class ClientInterface(AbstractAsyncContextManager["ClientInterface"], ABC):
    """Base interface class for implementing client connections.

    This abstract class defines the required methods that any client implementation
    must provide for establishing and managing connections to data sources.

    A client is an async context manager: ``async with`` calls ``close()`` on
    exit, including when the block raises. Prefer it over calling ``close()``
    by hand::

        async with MyClient() as client:
            await client.load(credentials=credentials)
            ...
    """

    @abstractmethod
    async def load(self, *args: Any, **kwargs: Any) -> None:
        """Establish the client connection.

        This method should handle the initialization and connection setup
        for the specific client implementation.

        Raises:
            AbstractClientLoadError: If the subclass does not implement this method.
        """

        raise AbstractClientLoadError()

    async def close(self, *args: Any, **kwargs: Any) -> None:
        """Close the client connection.

        This method should properly terminate the connection and clean up
        any resources used by the client. By default, it does nothing.
        Subclasses should override this method if cleanup is needed, and
        call ``super().close()`` so the base class releases its resources.
        """
        return

    async def __aenter__(self) -> Self:
        """Return the client. ``load()`` is not called; it needs credentials."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the client, whether or not the block raised."""
        await self.close()
