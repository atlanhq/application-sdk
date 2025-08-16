"""Utilities for credential providers."""

import copy
from typing import Any, Dict

from application_sdk.common.error_codes import CommonError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.secretstore import SecretStore
from application_sdk.services.statestore import StateStore, StateType

logger = get_logger(__name__)


async def get_credentials(credential_guid: str) -> Dict[str, Any]:
    """
    Resolve credentials based on credential source.

    Args:
        credential_guid: The GUID of the credential to resolve

    Returns:
        Dict with resolved credentials

    Raises:
        CommonError: If credential resolution fails
    """

    async def _get_credentials_async(credential_guid: str) -> Dict[str, Any]:
        """Async helper function to perform async I/O operations."""
        credential_config = await StateStore.get_state(
            credential_guid, StateType.CREDENTIALS
        )

        # Fetch secret data from secret store
        secret_key = credential_config.get("secret-path", credential_guid)
        secret_data = SecretStore.get_secret(secret_key=secret_key)

        # Resolve credentials
        credential_source = credential_config.get("credentialSource", "direct")
        if credential_source == "direct":
            credential_config.update(secret_data)
            return credential_config
        else:
            return resolve_credentials(credential_config, secret_data)

    try:
        # Run async operations directly
        return await _get_credentials_async(credential_guid)
    except Exception as e:
        logger.error(f"Error resolving credentials: {str(e)}")
        raise CommonError(
            CommonError.CREDENTIALS_RESOLUTION_ERROR,
            f"Failed to resolve credentials: {str(e)}",
        )


def resolve_credentials(
    credential_config: Dict[str, Any], secret_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Resolve credentials
    """
    credentials = copy.deepcopy(credential_config)

    # Replace values with secret values
    for key, value in list(credentials.items()):
        if isinstance(value, str) and value in secret_data:
            credentials[key] = secret_data[value]

    # Apply the same substitution to the 'extra' dictionary if it exists
    if "extra" in credentials and isinstance(credentials["extra"], dict):
        for key, value in list(credentials["extra"].items()):
            if isinstance(value, str):
                if value in secret_data:
                    credentials["extra"][key] = secret_data[value]
                elif value in secret_data.get("extra", {}):
                    credentials["extra"][key] = secret_data["extra"][value]

    return credentials
