"""Secret store for the application."""

import os
from typing import Any, Dict

from application_sdk.common.error_codes import ApplicationFrameworkErrorCodes
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput

logger = get_logger(__name__)


class SecretStoreInput:
    SECRET_STORE_NAME = os.getenv("SECRET_STORE_NAME", "secretstore")

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
            logger.error(
                "Invalid credential GUID provided",
                error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.SECRET_STORE_ERROR,
            )
            raise ValueError("Invalid credential GUID provided.")
        try:
            return StateStoreInput.get_state(f"credential_{credential_guid}")
        except Exception as e:
            logger.error(
                f"Error extracting credentials: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.SECRET_STORE_ERROR,
            )
            raise e
