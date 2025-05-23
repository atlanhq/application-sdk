import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from application_sdk.common.credential_utils import (
    CredentialError,
    _process_secret_data,
    apply_secret_values,
    fetch_secret,
    resolve_credentials,
)

# Helper strategy for credentials dictionaries
credential_dict_strategy = st.dictionaries(
    keys=st.text(min_size=1),
    values=st.one_of(st.text(), st.integers(), st.booleans()),
    min_size=1,
)


class TestCredentialUtils:
    """Tests for credential utility functions."""

    @given(
        secret_data=st.dictionaries(
            keys=st.text(min_size=1), values=st.text(), min_size=1, max_size=10
        )
    )
    def test_process_secret_data_dict(self, secret_data: Dict[str, str]):
        """Test processing secret data when it's already a dictionary."""
        result = _process_secret_data(secret_data)
        assert result == secret_data

    def test_process_secret_data_json(self):
        """Test processing secret data when it contains JSON string."""
        nested_data = {"username": "test_user", "password": "test_pass"}
        secret_data = {"data": json.dumps(nested_data)}

        result = _process_secret_data(secret_data)
        assert result == nested_data

    def test_process_secret_data_invalid_json(self):
        """Test processing secret data with invalid JSON."""
        secret_data = {"data": "invalid json string"}
        result = _process_secret_data(secret_data)
        assert result == secret_data  # Should return original if JSON parsing fails

    @given(
        source_credentials=credential_dict_strategy,
        secret_data=credential_dict_strategy,
    )
    def test_apply_secret_values(
        self, source_credentials: Dict[str, Any], secret_data: Dict[str, Any]
    ):
        """Test applying secret values to source credentials."""
        # Create a copy of source credentials with values that exist in secret_data
        test_credentials = source_credentials.copy()

        # Add some keys that should be substituted
        secret_keys = list(secret_data.keys())
        for i in range(min(2, len(secret_keys))):
            if i < len(secret_keys):
                key = secret_keys[i]
                if key in secret_data:
                    test_credentials[f"test_{key}"] = key

        # Add an extra field with some substitutions
        extra_dict = {}
        for i in range(min(2, len(secret_keys))):
            if i < len(secret_keys):
                key = secret_keys[i]
                extra_dict[f"extra_{key}"] = key

        test_credentials["extra"] = extra_dict

        # Apply the substitutions
        result = apply_secret_values(test_credentials, secret_data)

        # Check that substitutions were made correctly
        for key, value in test_credentials.items():
            if key != "extra" and isinstance(value, str) and value in secret_data:
                assert result[key] == secret_data[value]

        # Check extra dict substitutions
        if "extra" in test_credentials:
            for key, value in test_credentials["extra"].items():
                if isinstance(value, str) and value in secret_data:
                    assert result["extra"][key] == secret_data[value]

    @pytest.mark.asyncio
    async def test_resolve_credentials_direct(self):
        """Test resolving credentials with direct source."""
        credentials = {
            "credentialSource": "direct",
            "username": "test_user",
            "password": "test_pass",
        }

        result = await resolve_credentials(credentials)
        assert result == credentials

    @pytest.mark.asyncio
    async def test_resolve_credentials_default_direct(self):
        """Test resolving credentials with no credentialSource (defaults to direct)."""
        credentials = {"username": "test_user", "password": "test_pass"}

        result = await resolve_credentials(credentials)
        assert result == credentials

    @pytest.mark.asyncio
    @patch("application_sdk.common.credential_utils.fetch_secret")
    async def test_resolve_credentials_with_secret_store(
        self, mock_fetch_secret: AsyncMock
    ):
        """Test resolving credentials using a secret store."""
        mock_fetch_secret.return_value = {
            "pg_username": "db_user",
            "pg_password": "db_pass",
        }

        credentials = {
            "credentialSource": "aws-secrets",
            "username": "pg_username",
            "password": "pg_password",
            "extra": {"secret_key": "postgres/test"},
        }

        result = await resolve_credentials(credentials)

        mock_fetch_secret.assert_called_once_with("aws-secrets", "postgres/test")
        assert result["username"] == "db_user"
        assert result["password"] == "db_pass"

    @pytest.mark.asyncio
    async def test_resolve_credentials_missing_secret_key(self):
        """Test resolving credentials with missing secret_key."""
        credentials = {"credentialSource": "aws-secrets", "extra": {}}

        with pytest.raises(CredentialError, match="secret_key is required in extra"):
            await resolve_credentials(credentials)

    @pytest.mark.asyncio
    async def test_resolve_credentials_no_extra(self):
        """Test resolving credentials with no extra field."""
        credentials = {"credentialSource": "aws-secrets"}

        with pytest.raises(CredentialError, match="secret_key is required in extra"):
            await resolve_credentials(credentials)

    @pytest.mark.asyncio
    @patch("dapr.clients.DaprClient")
    async def test_fetch_secret_success(self, mock_dapr_client: Mock):
        """Test successful secret fetching."""
        mock_client_instance = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client_instance

        mock_secret_response = MagicMock()
        mock_secret_response.secret = {"username": "test", "password": "secret"}
        mock_client_instance.get_secret.return_value = mock_secret_response

        result = await fetch_secret("test-component", "test-key")

        mock_client_instance.get_secret.assert_called_once_with(
            store_name="test-component", key="test-key"
        )
        assert result == {"username": "test", "password": "secret"}

    @pytest.mark.asyncio
    @patch("dapr.clients.DaprClient")
    async def test_fetch_secret_failure(self, mock_dapr_client: Mock):
        """Test failed secret fetching."""
        mock_client_instance = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client_instance
        mock_client_instance.get_secret.side_effect = Exception("Connection failed")

        with pytest.raises(Exception, match="Connection failed"):
            await fetch_secret("test-component", "test-key")

    @pytest.mark.asyncio
    @patch("application_sdk.common.credential_utils.fetch_secret")
    async def test_resolve_credentials_fetch_error(self, mock_fetch_secret: Mock):
        """Test resolving credentials when fetch_secret fails."""
        mock_fetch_secret.side_effect = Exception("Dapr connection failed")

        credentials = {
            "credentialSource": "aws-secrets",
            "extra": {"secret_key": "postgres/test"},
        }

        with pytest.raises(CredentialError, match="Failed to resolve credentials"):
            await resolve_credentials(credentials)
