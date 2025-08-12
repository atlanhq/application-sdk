import json
from typing import Any, Dict
from unittest.mock import MagicMock, Mock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from application_sdk.inputs.secretstore import SecretStoreInput

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
        result = SecretStoreInput._process_secret_data(secret_data)
        assert result == secret_data

    def test_process_secret_data_json(self):
        """Test processing secret data when it contains JSON string."""
        nested_data = {"username": "test_user", "password": "test_pass"}
        secret_data = {"data": json.dumps(nested_data)}

        result = SecretStoreInput._process_secret_data(secret_data)
        assert result == nested_data

    def test_process_secret_data_invalid_json(self):
        """Test processing secret data with invalid JSON."""
        secret_data = {"data": "invalid json string"}
        result = SecretStoreInput._process_secret_data(secret_data)
        assert result == secret_data  # Should return original if JSON parsing fails

    def test_apply_secret_values_simple(self):
        """Test applying secret values to source credentials with simple case."""
        source_credentials = {
            "username": "db_user_key",
            "password": "db_pass_key",
            "extra": {"database": "db_name_key"},
        }

        secret_data = {
            "db_user_key": "actual_username",
            "db_pass_key": "actual_password",
            "db_name_key": "actual_database",
        }

        result = SecretStoreInput.apply_secret_values(source_credentials, secret_data)

        assert result["username"] == "actual_username"
        assert result["password"] == "actual_password"
        assert result["extra"]["database"] == "actual_database"

    def test_apply_secret_values_no_substitution(self):
        """Test applying secret values when no substitution is needed."""
        source_credentials = {"username": "direct_user", "password": "direct_pass"}

        secret_data = {"some_key": "some_value"}

        result = SecretStoreInput.apply_secret_values(source_credentials, secret_data)

        # Should remain unchanged
        assert result == source_credentials

    @given(
        source_credentials=credential_dict_strategy,
        secret_data=credential_dict_strategy,
    )
    def test_apply_secret_values_property(
        self, source_credentials: Dict[str, Any], secret_data: Dict[str, Any]
    ):
        """Property-based test for apply_secret_values with safe data."""
        # Avoid overlapping keys/values that could cause circular references
        safe_secret_data = {f"secret_{k}": v for k, v in secret_data.items()}

        test_credentials = source_credentials.copy()

        # Only add substitutions for keys that exist in safe_secret_data
        secret_keys = list(safe_secret_data.keys())
        if secret_keys:
            # Add one substitution to test
            key_to_substitute = secret_keys[0]
            test_credentials["test_field"] = key_to_substitute

            # Add extra field
            test_credentials["extra"] = {"extra_field": key_to_substitute}

        result = SecretStoreInput.apply_secret_values(
            test_credentials, safe_secret_data
        )

        # Verify substitutions happened correctly
        if secret_keys and "test_field" in test_credentials:
            expected_value = safe_secret_data[test_credentials["test_field"]]
            assert result["test_field"] == expected_value
            assert result["extra"]["extra_field"] == expected_value

    @patch("application_sdk.inputs.objectstore.DaprClient")
    @patch("application_sdk.inputs.statestore.StateStoreInput.get_state")
    @patch("application_sdk.inputs.secretstore.DaprClient")
    @patch("application_sdk.inputs.secretstore.DEPLOYMENT_NAME", "production")
    def test_fetch_secret_success(
        self, mock_secret_dapr_client, mock_get_state, mock_object_dapr_client
    ):
        """Test successful secret fetching."""
        # Setup mock for secret store
        mock_client = MagicMock()
        mock_secret_dapr_client.return_value.__enter__.return_value = mock_client

        # Mock the secret response
        mock_response = MagicMock()
        mock_response.secret = {"username": "test", "password": "secret"}
        mock_client.get_secret.return_value = mock_response

        # Mock the state store response
        mock_get_state.return_value = {"additional_key": "additional_value"}

        result = SecretStoreInput.get_secret(
            "test-key", component_name="test-component"
        )

        # Verify the result includes both secret and state data
        expected_result = {
            "username": "test",
            "password": "secret",
        }
        assert result == expected_result
        mock_client.get_secret.assert_called_once_with(
            store_name="test-component", key="test-key"
        )

    @patch("application_sdk.inputs.objectstore.DaprClient")
    @patch("application_sdk.inputs.statestore.StateStoreInput.get_state")
    @patch("application_sdk.inputs.secretstore.DaprClient")
    @patch("application_sdk.inputs.secretstore.DEPLOYMENT_NAME", "production")
    def test_fetch_secret_failure(
        self,
        mock_secret_dapr_client: Mock,
        mock_get_state: Mock,
        mock_object_dapr_client: Mock,
    ):
        """Test failed secret fetching."""
        mock_client = MagicMock()
        mock_secret_dapr_client.return_value.__enter__.return_value = mock_client
        mock_client.get_secret.side_effect = Exception("Connection failed")

        # Mock the state store (though it won't be reached due to the exception)
        mock_get_state.return_value = {}

        with pytest.raises(Exception, match="Connection failed"):
            SecretStoreInput.get_secret("test-key", component_name="test-component")
