from abc import ABC, abstractmethod
from typing import Any


class HandlerInterface(ABC):
    """
    Abstract base class for workflow handlers
    """

    @abstractmethod
    async def load(self, *args: Any, **kwargs: Any) -> None:
        """
        Method to load the handler
        """
        pass

    @abstractmethod
    async def test_auth(self, *args: Any, **kwargs: Any) -> bool:
        """
        Abstract method to test the authentication credentials
        To be implemented by the subclass
        """
        raise NotImplementedError("test_auth method not implemented")

    @abstractmethod
    async def preflight_check(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to perform preflight checks
        To be implemented by the subclass
        """
        raise NotImplementedError("preflight_check method not implemented")

    @abstractmethod
    async def fetch_metadata(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to fetch metadata
        To be implemented by the subclass
        """
        raise NotImplementedError("fetch_metadata method not implemented")
