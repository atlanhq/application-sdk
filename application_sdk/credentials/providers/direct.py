"""Direct credential provider implementation."""

from typing import Any, Dict

from application_sdk.credentials.base import CredentialProvider


class DirectCredentialProvider(CredentialProvider):
    """Provider for directly supplied credentials."""

    async def get_credentials(
        self, source_credentials: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Return the direct credentials as is.

        Args:
            source_credentials (Dict[str, Any]): The direct credentials.

        Returns:
            Dict[str, Any]: The same credentials.
        """
        return source_credentials
