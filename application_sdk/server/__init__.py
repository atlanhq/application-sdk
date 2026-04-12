"""Base interface for API servers.

This module provides the abstract base class for all API servers,
defining the core interface that all server types must implement.
"""

from abc import ABC, abstractmethod
from typing import Optional

from application_sdk.handler.base import Handler


class ServerInterface(ABC):
    """Abstract base class for API servers.

    This class defines the interface that all API servers must implement,
    providing a standardized way to handle server lifecycle and configuration.

    Attributes:
        handler (Optional[Handler]): The handler instance for processing
            application-specific operations. Can be None if no handler is needed.
    """

    handler: Optional[Handler]

    def __init__(
        self,
        handler: Optional[Handler] = None,
    ):
        """Initialize the API server.

        Args:
            handler (Optional[Handler], optional): The handler instance for
                processing server-specific operations. Defaults to None.
        """
        self.handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Start the server.

        This abstract method must be implemented by subclasses to define the
        server-specific startup logic. The implementation should handle
        all necessary initialization and startup procedures.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("start method not implemented")
