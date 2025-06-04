"""Utilities for credential providers."""

from typing import Any, Dict

from application_sdk.common.error_codes import CommonError
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


async def resolve_credentials(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve credentials based on credential source.

    Args:
        credentials: Source credentials containing:
            - credentialSource: "direct" or component name
            - extra.secret_key: Secret path/key to fetch

    Returns:
        Dict with resolved credentials

    Raises:
        CommonError: If credential resolution fails
    """
    credential_source = credentials.get("credentialSource", "direct")

    # If direct, return as-is
    if credential_source == "direct":
        return credentials

    # Otherwise, treat as Dapr component name
    try:
        # Extract secret key from credentials extra
        extra = credentials.get("extra", {})
        secret_key = extra.get("secret_key")

        if not secret_key:
            raise CommonError(
                CommonError.CREDENTIALS_RESOLUTION_ERROR,
                "secret_key is required in extra",
            )

        # Fetch and apply secret using SecretStoreInput
        secret_data = await SecretStoreInput.fetch_secret(credential_source, secret_key)
        return SecretStoreInput.apply_secret_values(credentials, secret_data)

    except Exception as e:
        logger.error(f"Error resolving credentials: {str(e)}")
        raise CommonError(
            CommonError.CREDENTIALS_RESOLUTION_ERROR,
            f"Failed to resolve credentials: {str(e)}",
        )
