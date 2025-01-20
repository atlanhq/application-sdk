from abc import ABC, abstractmethod


class ClientInterface(ABC):
    """Base interface class for implementing client connections.

    This abstract class defines the required methods that any client implementation
    must provide for establishing and managing connections to data sources.
    """

    def __init__(self):
        """Initialize a new client instance."""
        pass

    @abstractmethod
    async def load(self):
        """Establish the client connection.

        This method should handle the initialization and connection setup
        for the specific client implementation.
        """
        pass

    @abstractmethod
    async def close(self):
        """Close the client connection.

        This method should properly terminate the connection and clean up
        any resources used by the client.
        """
        pass
