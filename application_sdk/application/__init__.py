"""Base application interface for Atlan applications.

This module provides the abstract base class for all Atlan applications,
defining the core interface that all application types must implement.
"""

from abc import ABC, abstractmethod
from typing import Optional

from application_sdk.handlers import HandlerInterface


class AtlanApplicationInterface(ABC):
    """Abstract base class for Atlan applications.

    This class defines the interface that all Atlan applications must implement,
    providing a standardized way to handle application lifecycle and configuration.

    Attributes:
        handler (Optional[HandlerInterface]): The handler instance for processing
            application-specific operations. Can be None if no handler is needed.
    """

    handler: Optional[HandlerInterface]

    def __init__(
        self,
        handler: Optional[HandlerInterface] = None,
    ):
        """Initialize the Atlan application.

        Args:
            handler (Optional[HandlerInterface], optional): The handler instance for
                processing application-specific operations. Defaults to None.
        """
        self.handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Start the application.

        This abstract method must be implemented by subclasses to define the
        application-specific startup logic. The implementation should handle
        all necessary initialization and startup procedures.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("start method not implemented")
