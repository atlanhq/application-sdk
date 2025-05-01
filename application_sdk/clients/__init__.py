"""Base client interfaces for Atlan applications.

This module provides the abstract base class for all client implementations,
defining the core interface that any client must implement for connecting
to external services and data sources.
"""

from abc import ABC, abstractmethod
from typing import Any


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
