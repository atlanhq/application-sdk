from abc import ABC, abstractmethod


class AtlanApplicationInterface(ABC):
    """
    Atlan Application Interface class
    """

    @abstractmethod
    async def start(self) -> None:
        """
        Method to start the application
        To be implemented by the subclass by writing custom logic to start the application
        based on the application type
        """
        raise NotImplementedError("start method not implemented")
