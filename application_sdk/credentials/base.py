"""Base definitions for credential providers."""

from typing import Any, Dict, Protocol


class CredentialError(Exception):
    """Base exception for credential-related errors."""
    pass


class CredentialProvider(Protocol):
    """Protocol defining the interface for credential providers."""

    async def get_credentials(self, source_credentials: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get the resolved credentials from the source credentials.

        Args:
            source_credentials (Dict[str, Any]): The source credentials containing
                metadata necessary to resolve actual credentials.

        Returns:
            Dict[str, Any]: The resolved credentials.
        
        Raises:
            CredentialError: If credentials cannot be resolved.
        """
        ...