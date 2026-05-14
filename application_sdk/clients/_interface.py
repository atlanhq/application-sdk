from abc import ABC, abstractmethod
from typing import Any

from application_sdk.clients._sql_errors import AbstractClientLoadError


class ClientInterface(ABC):
    """Base interface class for implementing client connections.

    This abstract class defines the required methods that any client implementation
    must provide for establishing and managing connections to data sources.
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
        Subclasses should override this method if cleanup is needed.
        """
        return
