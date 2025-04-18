"""Secret store for the application."""

from typing import Any, Dict

from application_sdk.inputs.statestore import StateStoreInput


class SecretStoreInput:
    @classmethod
    def extract_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """Extract credentials from the state store using the credential GUID.

        Args:
            credential_guid: The unique identifier for the credentials.

        Returns:
            Dict[str, Any]: The credentials if found.

        Raises:
            ValueError: If the credential_guid is invalid or credentials are not found.
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> SecretStoreInput.extract_credentials("1234567890")
            {"username": "admin", "password": "password"}
        """
        if not credential_guid:
            raise ValueError("Invalid credential GUID provided.")
        return StateStoreInput.get_state(f"credential_{credential_guid}")
