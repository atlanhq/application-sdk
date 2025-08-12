from typing import Optional

from pyatlan.client.atlan import AtlanClient

from application_sdk.common.error_codes import ClientError
from application_sdk.constants import (
    ATLAN_API_KEY,
    ATLAN_API_TOKEN_GUID,
    ATLAN_BASE_URL,
    ATLAN_CLIENT_ID,
    ATLAN_CLIENT_SECRET,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def get_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    api_token_guid: Optional[str] = None,
) -> AtlanClient:
    """
    Returns an authenticated AtlanClient instance using provided parameters or environment variables.

    Selects authentication method based on the presence of parameters or environment variables and validates the required configuration.
    In general, the use of environment variables is recommended. Any parameters specified will override the environment variables.

    Args:
    base_url: Atlan base URL (overrides ATLAN_BASE_URL)
    api_key: Atlan API key (overrides ATLAN_API_KEY)
    api_token_guid: API token GUID (overrides API_TOKEN_GUID)
    """
    # Resolve final values (parameters override env vars)
    final_token_guid = api_token_guid or ATLAN_API_TOKEN_GUID
    final_base_url = base_url or ATLAN_BASE_URL
    final_api_key = api_key or ATLAN_API_KEY

    # Priority 1: Token-based auth (recommended for production)
    if final_token_guid:
        if final_base_url or final_api_key:  # Check original params, not env vars
            logger.warning(
                "Token auth takes precedence - ignoring base_url/api_key parameters as well as ATLAN_BASE_URL and ATLAN_API_KEY environment variables."
            )
        return _get_client_from_token(final_token_guid)

    # Priority 2: API key + base URL auth
    if not final_base_url:
        raise ClientError(
            "ATLAN_BASE_URL is required (via parameter or environment variable)"
        )
    if not final_api_key:
        raise ClientError(
            "ATLAN_API_KEY is required (via parameter or environment variable)"
        )

    logger.info("Using API key-based authentication")
    return AtlanClient(base_url=final_base_url, api_key=final_api_key)


def _get_client_from_token(api_token_guid: str):
    if not ATLAN_CLIENT_ID:
        raise ClientError(
            f"{ClientError.AUTH_CONFIG_ERROR}: Environment variable CLIENT_ID is required when API_TOKEN_GUID is set."
        )
    if not ATLAN_CLIENT_SECRET:
        raise ClientError(
            f"{ClientError.AUTH_CONFIG_ERROR}: Environment variable CLIENT_SECRET is required when API_TOKEN_GUID is set."
        )
    return AtlanClient.from_token_guid(guid=api_token_guid)
