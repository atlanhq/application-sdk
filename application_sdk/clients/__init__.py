from abc import ABC, abstractmethod


class ClientInterface(ABC):
    """Base interface class for implementing client connections.

    This abstract class defines the required methods that any client implementation
    must provide for establishing and managing connections to data sources.
    """

    @abstractmethod
    async def load(self):
        """Establish the client connection.

        This method should handle the initialization and connection setup
        for the specific client implementation.
        """
        raise NotImplementedError("load method is not implemented")

    async def close(self):
        """Close the client connection.

        This method should properly terminate the connection and clean up
        any resources used by the client.
        """
        return
