import os
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.clients.paatlan import get_client
from application_sdk.common.error_codes import ClientError


@pytest.mark.parametrize(
    "params,env_vars,raises,",
    [
        (
            {"base_url": None, "api_key": "api_key_789", "api_token_guid": None},
            {},
            "base_url or environment variable ATLAN_BASE is required when api_key is provided",
        ),
        # Error: missing api_key (param and env)
        (
            {"base_url": "https://atlan.com", "api_key": None, "api_token_guid": None},
            {},
            "api_key or environment variable ATLAN_API_KEY is required",
        ),
        # Error: missing both base_url and api_key (param and env)
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {},
            "base_url parameter or environment variable ATLAN_BASE_URL is required",
        ),
        # Error: missing client_id
        (
            {"base_url": None, "api_key": None, "api_token_guid": "123"},
            {},
            "Environment variable CLIENT_ID is required when API_TOKEN_GUID is set",
        ),
        # Error: missing client_secret
        (
            {"base_url": None, "api_key": None, "api_token_guid": "123"},
            {"CLIENT_ID": "123"},
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
def test_get_client_parameter_problems(params, env_vars, raises):
    # Arrange
    patch_env = {**env_vars}
    with patch.dict(os.environ, patch_env, clear=True):
        # Act / Assert
        with pytest.raises(ClientError) as excinfo:
            get_client(**params)
            # Assert
        assert raises in str(excinfo.value)


@pytest.mark.parametrize(
    "params,env_vars,expected_call,expected_args,raises,case_id",
    [
        # Happy path: api_token_guid param provided
        (
            {"base_url": None, "api_key": None, "api_token_guid": "guid_param"},
            {},
            "_get_client_from_token",
            ("guid_param",),
            None,
            "param_token_guid",
        ),
        # Happy path: API_TOKEN_GUID env provided
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {"API_TOKEN_GUID": "guid_env"},
            "_get_client_from_token",
            ("guid_env",),
            None,
            "env_token_guid",
        ),
        # Happy path: base_url and api_key params provided
        (
            {
                "base_url": "https://atlan.com",
                "api_key": "api_key_123",
                "api_token_guid": None,
            },
            {},
            "AtlanClient",
            {"base_url": "https://atlan.com", "api_key": "api_key_123"},
            None,
            "param_base_url_and_api_key",
        ),
        # Happy path: env base_url and api_key
        (
            {"base_url": None, "api_key": None, "api_token_guid": None},
            {"ATLAN_BASE_URL": "https://env.atlan.com", "ATLAN_API_KEY": "env_api_key"},
            "AtlanClient",
            {},
            None,
            "env_base_url_and_api_key",
        ),
        # Edge case: both param and env for base_url and api_key, param takes precedence
        (
            {
                "base_url": "https://param.atlan.com",
                "api_key": "param_api_key",
                "api_token_guid": None,
            },
            {"ATLAN_BASE_URL": "https://env.atlan.com", "ATLAN_API_KEY": "env_api_key"},
            "AtlanClient",
            {"base_url": "https://param.atlan.com", "api_key": "param_api_key"},
            None,
            "param_precedence_over_env",
        ),
    ],
    ids=[
        "param_token_guid",
        "env_token_guid",
        "param_base_url_and_api_key",
        "env_base_url_and_api_key",
        "param_precedence_over_env",
    ],
)
def test_get_client_all_cases(
    params, env_vars, expected_call, expected_args, raises, case_id
):
    # Arrange
    patch_env = {**env_vars}
    with patch.dict(os.environ, patch_env, clear=True):
        with (
            patch(
                "application_sdk.clients.paatlan._get_client_from_token"
            ) as mock_get_client_from_token,
            patch("application_sdk.clients.paatlan.AtlanClient") as mock_atlan_client,
        ):
            mock_client_instance = MagicMock()
            mock_get_client_from_token.return_value = mock_client_instance
            mock_atlan_client.return_value = mock_client_instance
            result = get_client(**params)
            # Assert
            if expected_call == "_get_client_from_token":
                mock_get_client_from_token.assert_called_once_with(*expected_args)
                assert result == mock_client_instance
            elif expected_call == "AtlanClient":
                # Check correct call args
                if expected_args:
                    mock_atlan_client.assert_called_once_with(**expected_args)
                else:
                    mock_atlan_client.assert_called_once_with()
                assert result == mock_client_instance
