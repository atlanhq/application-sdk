import os
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.clients.atlan_client import get_client
from application_sdk.common.error_codes import ClientError


@pytest.mark.parametrize(
    "params,env_vars,msg,case_id",
    [
        # Error: missing base_url
        (
            {"base_url": None, "api_key": "api_key_789", "api_token_guid": None},
            {},
            "ATLAN_BASE_URL is required",
            "missing_base_url",
        ),
        # Error: missing api_key
        (
            {"base_url": "https://atlan.com", "api_key": None, "api_token_guid": None},
            {},
            "ATLAN_API_KEY is required",
            "missing_api_key",
        ),
        # Error: missing both base_url and api_key
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {},
            "ATLAN_BASE_URL is required",
            "missing_both_base_url_and_api_key",
        ),
        # Error: missing client_id
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {},
            "Environment variable CLIENT_ID is required when API_TOKEN_GUID is set",
            "missing_client_id",
        ),
        # Error: missing client_secret
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {"CLIENT_ID": "CLIENT_ID_789"},
            "Environment variable CLIENT_SECRET is required when API_TOKEN_GUID is set",
            "missing_client_id",
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
def test_get_client_bad_params(params, env_vars, msg, case_id):
    # Arrange
    patch_env = {**env_vars}

    with patch.dict(os.environ, patch_env, clear=True):
        # Act / Assert
        with pytest.raises(ClientError) as excinfo:
            get_client(**params)
        # Assert
        assert msg in str(excinfo.value)


@pytest.mark.parametrize(
    "params,env_vars,expected_call,expected_args,expected_log,raises,case_id",
    [
        # Happy path: token auth via param
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {},
            "_get_client_from_token",
            ("guid_param",),
            None,
            None,
            "param_token_guid",
        ),
        # Happy path: token auth via env
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {"API_TOKEN_GUID": "guid_env"},
            "_get_client_from_token",
            ("guid_env",),
            None,
            None,
            "env_token_guid",
        ),
        # Happy path: token auth, base_url/api_key params present (should log warning)
        (
            {
                "base_url": "https://atlan.com",
                "api_key": "api_key_123",
                "api_token_guid": "guid_param",
            },
            {},
            "_get_client_from_token",
            ("guid_param",),
            "warning",
            None,
            "token_guid_with_base_url_api_key",
        ),
    ],
    ids=[
        "param_token_guid",
        "env_token_guid",
        "token_guid_with_base_url_api_key",
    ],
)
def test_get_client_with_token_guid(
    params, env_vars, expected_call, expected_args, expected_log, raises, case_id
):
    # Arrange
    patch_env = {**env_vars}

    with patch.dict(os.environ, patch_env, clear=True):
        with patch(
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
    "params,env_vars,expected_call,expected_args,expected_log,raises,case_id",
    [
        # Happy path: API key auth via params
        (
            {
                "base_url": "https://atlan.com",
                "api_key": "api_key_123",
                "api_token_guid": None,
            },
            {},
            "AtlanClient",
            {"base_url": "https://atlan.com", "api_key": "api_key_123"},
            "info",
            None,
            "param_base_url_and_api_key",
        ),
        # Happy path: API key auth via env
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {"ATLAN_BASE_URL": "https://env.atlan.com", "ATLAN_API_KEY": "env_api_key"},
            "AtlanClient",
            {"base_url": "https://env.atlan.com", "api_key": "env_api_key"},
            "info",
            None,
            "env_base_url_and_api_key",
        ),
        # Edge case: param overrides env
        (
            {
                "base_url": "https://param.atlan.com",
                "api_key": "param_api_key",
                "api_token_guid": None,
            },
            {"ATLAN_BASE_URL": "https://env.atlan.com", "ATLAN_API_KEY": "env_api_key"},
            "AtlanClient",
            {"base_url": "https://param.atlan.com", "api_key": "param_api_key"},
            "info",
            None,
            "param_precedence_over_env",
        ),
    ],
    ids=[
        "param_base_url_and_api_key",
        "env_base_url_and_api_key",
        "param_precedence_over_env",
    ],
)
def test_get_client_with_api_key(
    params, env_vars, expected_call, expected_args, expected_log, raises, case_id
):
    # Arrange
    patch_env = {**env_vars}

    with patch.dict(os.environ, patch_env, clear=True):
        with patch(
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
