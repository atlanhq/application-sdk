import os
from typing import Optional

from pyatlan.client.atlan import AtlanClient

from application_sdk.common.error_codes import ClientError


def get_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    api_token_guid: Optional[str] = None,
) -> AtlanClient:
    """Returns an authenticated AtlanClient instance using provided parameters or environment variables.

    Selects authentication method based on the presence of parameters or environment variables and validates required configuration.

    Args:
        base_url (Optional[str]): The Atlan base URL. If not provided, will use the ATLAN_BASE_URL environment variable.
        api_key (Optional[str]): The Atlan API key. If not provided, will use the ATLAN_API_KEY environment variable.
        api_token_guid (Optional[str]): The Atlan API token GUID. If not provided, will use the API_TOKEN_GUID environment variable.

    Returns:
        AtlanClient: An authenticated AtlanClient instance.

    Raises:
        ClientError: If required parameters or environment variables are missing for the selected authentication method.
    """
    if api_token_guid:
        return _get_client_from_token(api_token_guid)
    elif api_token_guid := os.environ.get("API_TOKEN_GUID"):
        return _get_client_from_token(api_token_guid)
    if not base_url and not (os.environ.get("ATLAN_BASE_URL")):
        raise ClientError(
            f"{ClientError.AUTH_CONFIG_ERROR}: base_url parameter or environment variable ATLAN_BASE_URL is required when API_TOKEN_GUID is not set."
        )
    if not api_key and not (os.environ.get("ATLAN_API_KEY")):
        raise ClientError(
            f"{ClientError.AUTH_CONFIG_ERROR}: api_key or environment variable ATLAN_API_KEY is required when API_TOKEN_GUID is not set."
        )
    if base_url:
        if api_key:
            return AtlanClient(base_url=base_url, api_key=api_key)
        else:
            return AtlanClient(base_url=base_url)
    elif api_key:
        return AtlanClient(api_key=api_key)
    return AtlanClient()


def _get_client_from_token(api_token_guid):
    if not (os.getenv("CLIENT_ID")):
        raise ClientError(
            f"{ClientError.AUTH_CONFIG_ERROR}: Environment variable CLIENT_ID is required when API_TOKEN_GUID is set."
        )
    if not (os.getenv("CLIENT_SECRET")):
        raise ClientError(
            f"{ClientError.AUTH_CONFIG_ERROR}: Environment variable CLIENT_SECRET is required when API_TOKEN_GUID is set."
        )
    return AtlanClient.from_token_guid(guid=api_token_guid)
