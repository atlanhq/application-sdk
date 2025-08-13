from unittest.mock import MagicMock, patch

import pytest

from application_sdk.clients.atlan_client import get_client
from application_sdk.common.error_codes import ClientError


@pytest.mark.parametrize(
    "params,constants,msg",
    [
        # Error: missing base_url
        (
            {"base_url": None, "api_key": "api_key_789", "api_token_guid": None},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": None,
                "ATLAN_CLIENT_SECRET": None,
            },
            "ATLAN_BASE_URL is required",
        ),
        # Error: missing api_key
        (
            {"base_url": "https://atlan.com", "api_key": None, "api_token_guid": None},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": None,
                "ATLAN_CLIENT_SECRET": None,
            },
            "ATLAN_API_KEY is required",
        ),
        # Error: missing both base_url and api_key
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": None,
                "ATLAN_CLIENT_SECRET": None,
            },
            "ATLAN_BASE_URL is required",
        ),
        # Error: missing client_id
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": None,
                "ATLAN_CLIENT_SECRET": None,
            },
            "Environment variable CLIENT_ID is required when API_TOKEN_GUID is set",
        ),
        # Error: missing client_secret
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": "CLIENT_ID_789",
                "ATLAN_CLIENT_SECRET": None,
            },
            "Environment variable CLIENT_SECRET is required when API_TOKEN_GUID is set",
        ),
    ],
    ids=[
        "missing_base_url",
        "missing_api_key",
        "missing_both_base_url_and_api_key",
        "missing_client_id",
        "missing_client_secret",
    ],
)
def test_get_client_bad_params(params, constants, msg):
    # Arrange
    with patch(
        "application_sdk.clients.atlan_client.ATLAN_API_TOKEN_GUID",
        constants["ATLAN_API_TOKEN_GUID"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_BASE_URL",
        constants["ATLAN_BASE_URL"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_API_KEY", constants["ATLAN_API_KEY"]
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_CLIENT_ID",
        constants["ATLAN_CLIENT_ID"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_CLIENT_SECRET",
        constants["ATLAN_CLIENT_SECRET"],
    ):
        # Act / Assert
        with pytest.raises(ClientError) as excinfo:
            get_client(**params)
        # Assert
        assert msg in str(excinfo.value)


@pytest.mark.parametrize(
    "params,constants,expected_call,expected_args,expected_log,raises",
    [
        # Happy path: token auth via param
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": "cid",
                "ATLAN_CLIENT_SECRET": "csecret",
            },
            "_get_client_from_token",
            ("guid_param",),
            None,
            None,
        ),
        # Happy path: token auth via env
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {
                "ATLAN_API_TOKEN_GUID": "guid_const",
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": "cid",
                "ATLAN_CLIENT_SECRET": "csecret",
            },
            "_get_client_from_token",
            ("guid_const",),
            None,
            None,
        ),
        # Happy path: token auth, base_url/api_key params present (should log warning)
        (
            {
                "base_url": "https://atlan.com",
                "api_key": "api_key_123",
                "api_token_guid": "guid_param",
            },
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": "cid",
                "ATLAN_CLIENT_SECRET": "csecret",
            },
            "_get_client_from_token",
            ("guid_param",),
            "warning",
            None,
        ),
    ],
    ids=[
        "param_token_guid",
        "env_token_guid",
        "token_guid_with_base_url_api_key",
    ],
)
def test_get_client_with_token_guid(
    params, constants, expected_call, expected_args, expected_log, raises
):
    # Arrange

    with patch(
        "application_sdk.clients.atlan_client.ATLAN_API_TOKEN_GUID",
        constants["ATLAN_API_TOKEN_GUID"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_BASE_URL",
        constants["ATLAN_BASE_URL"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_API_KEY", constants["ATLAN_API_KEY"]
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_CLIENT_ID",
        constants["ATLAN_CLIENT_ID"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_CLIENT_SECRET",
        constants["ATLAN_CLIENT_SECRET"],
    ), patch(
        "application_sdk.clients.atlan_client._get_client_from_token"
    ) as mock_get_client_from_token, patch(
        "application_sdk.clients.atlan_client.AtlanClient"
    ) as mock_atlan_client, patch(
        "application_sdk.clients.atlan_client.logger"
    ) as mock_logger:
        mock_client_instance = MagicMock()
        mock_get_client_from_token.return_value = mock_client_instance
        mock_atlan_client.return_value = mock_client_instance

        # Act & Assert
        # Act
        result = get_client(**params)
        # Assert
        mock_get_client_from_token.assert_called_once_with(*expected_args)
        assert result == mock_client_instance
        if expected_log == "warning":
            mock_logger.warning.assert_called_once()


@pytest.mark.parametrize(
    "params,constants,expected_call,expected_args,expected_log,raises",
    [
        # Happy path: API key auth via params
        (
            {
                "base_url": "https://atlan.com",
                "api_key": "api_key_123",
                "api_token_guid": None,
            },
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": None,
                "ATLAN_API_KEY": None,
                "ATLAN_CLIENT_ID": "cid",
                "ATLAN_CLIENT_SECRET": "csecret",
            },
            "AtlanClient",
            {"base_url": "https://atlan.com", "api_key": "api_key_123"},
            "info",
            None,
        ),
        # Happy path: API key auth via constants
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": "https://const.atlan.com",
                "ATLAN_API_KEY": "const_api_key",
                "ATLAN_CLIENT_ID": None,
                "ATLAN_CLIENT_SECRET": None,
            },
            "AtlanClient",
            {"base_url": "https://const.atlan.com", "api_key": "const_api_key"},
            "info",
            None,
        ),
        # Edge case: param overrides env
        (
            {
                "base_url": "https://param.atlan.com",
                "api_key": "param_api_key",
                "api_token_guid": None,
            },
            {
                "ATLAN_API_TOKEN_GUID": None,
                "ATLAN_BASE_URL": "https://const.atlan.com",
                "ATLAN_API_KEY": "const_api_key",
                "ATLAN_CLIENT_ID": None,
                "ATLAN_CLIENT_SECRET": None,
            },
            "AtlanClient",
            {"base_url": "https://param.atlan.com", "api_key": "param_api_key"},
            "info",
            None,
        ),
    ],
    ids=[
        "param_base_url_and_api_key",
        "env_base_url_and_api_key",
        "param_precedence_over_env",
    ],
)
def test_get_client_with_api_key(
    params, constants, expected_call, expected_args, expected_log, raises
):
    # Arrange
    with patch(
        "application_sdk.clients.atlan_client.ATLAN_API_TOKEN_GUID",
        constants["ATLAN_API_TOKEN_GUID"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_BASE_URL",
        constants["ATLAN_BASE_URL"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_API_KEY", constants["ATLAN_API_KEY"]
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_CLIENT_ID",
        constants["ATLAN_CLIENT_ID"],
    ), patch(
        "application_sdk.clients.atlan_client.ATLAN_CLIENT_SECRET",
        constants["ATLAN_CLIENT_SECRET"],
    ), patch(
        "application_sdk.clients.atlan_client._get_client_from_token"
    ) as mock_get_client_from_token, patch(
        "application_sdk.clients.atlan_client.AtlanClient"
    ) as mock_atlan_client, patch(
        "application_sdk.clients.atlan_client.logger"
    ) as mock_logger:
        mock_client_instance = MagicMock()
        mock_get_client_from_token.return_value = mock_client_instance
        mock_atlan_client.return_value = mock_client_instance

        # Act & Assert
        # Act
        result = get_client(**params)
        # Assert
        mock_atlan_client.assert_called_once_with(**expected_args)
        assert result == mock_client_instance
        if expected_log == "info":
            mock_logger.info.assert_called_once()
