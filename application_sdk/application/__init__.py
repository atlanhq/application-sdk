from abc import ABC, abstractmethod
from typing import Optional

from application_sdk.handlers import HandlerInterface


class AtlanApplicationInterface(ABC):
    """
    Atlan Application Interface class
    """

    handler: Optional[HandlerInterface]

    def __init__(
        self,
        handler: Optional[HandlerInterface] = None,
    ):
        self.handler = handler

    @abstractmethod
    async def start(self) -> None:
        """
        Method to start the application
        To be implemented by the subclass by writing custom logic to start the application
        based on the application type
        """
        raise NotImplementedError("start method not implemented")
