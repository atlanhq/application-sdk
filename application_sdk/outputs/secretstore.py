"""Secret store for the application."""

import uuid
from typing import Any, Dict

from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT
from application_sdk.inputs.statestore import StateType
from application_sdk.outputs.statestore import StateStoreOutput


class SecretStoreOutput:
    @classmethod
    async def save_secret(cls, config: Dict[str, Any]) -> str:
        """Store credentials in the state store.

        Args:
            config: The credentials to store.

        Returns:
            str: The generated credential GUID.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> SecretStoreOutput.save_secret({"username": "admin", "password": "password"})
            "1234567890"
        """
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            # NOTE: (development) temporary solution to store the credentials in the state store.
            # In production, dapr doesn't support creating secrets.
            credential_guid = str(uuid.uuid4())
            await StateStoreOutput.save_state_object(
                id=credential_guid, value=config, type=StateType.CREDENTIALS
            )
            return credential_guid
        else:
            raise ValueError("Storing credentials is not supported in production.")
