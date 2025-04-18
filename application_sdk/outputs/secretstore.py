"""Secret store for the application."""

import uuid
from typing import Any, Dict

from application_sdk.outputs.statestore import StateStoreOutput


class SecretStoreOutput:
    @classmethod
    def store_credentials(cls, config: Dict[str, Any]) -> str:
        """Store credentials in the state store.

        Args:
            config: The credentials to store.

        Returns:
            str: The generated credential GUID.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> SecretStoreOutput.store_credentials({"username": "admin", "password": "password"})
            "credential_1234567890"
        """
        credential_guid = str(uuid.uuid4())
        StateStoreOutput.save_state(f"credential_{credential_guid}", config)
        return credential_guid
