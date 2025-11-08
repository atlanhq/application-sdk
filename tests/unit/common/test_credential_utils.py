import json
from typing import Any, Dict
from unittest.mock import MagicMock, Mock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from application_sdk.common.error_codes import CommonError
from application_sdk.services.secretstore import SecretStore
from application_sdk.services.statestore import StateType

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
            keys=st.text(min_size=1), values=st.text(), min_size=2, max_size=10
        )
    )
    def test_process_secret_data_dict(self, secret_data: Dict[str, str]):
        """Test processing secret data when it's already a dictionary with multiple keys."""
        result = SecretStore._process_secret_data(secret_data)
        assert result == secret_data

    def test_process_secret_data_json(self):
        """Test processing secret data when it contains JSON string."""
        nested_data = {"username": "test_user", "password": "test_pass"}
        secret_data = {"data": json.dumps(nested_data)}

        result = SecretStore._process_secret_data(secret_data)
        assert result == nested_data

    def test_process_secret_data_single_key_json_parsing(self):
        """Test that single-key dictionaries with JSON string values are parsed."""
        # Test case that was failing: single key with empty JSON object
        secret_data = {"0": "{}"}
        result = SecretStore._process_secret_data(secret_data)
        assert result == {}

        # Test case: single key with JSON object
        secret_data = {"key": '{"username": "test", "password": "secret"}'}
        result = SecretStore._process_secret_data(secret_data)
        assert result == {"username": "test", "password": "secret"}

        # Test case: single key with non-JSON string (should remain unchanged)
        secret_data = {"key": "not json"}
        result = SecretStore._process_secret_data(secret_data)
        assert result == secret_data

    def test_process_secret_data_invalid_json(self):
        """Test processing secret data with invalid JSON."""
        secret_data = {"data": "invalid json string"}
        result = SecretStore._process_secret_data(secret_data)
        assert result == secret_data  # Should return original if JSON parsing fails

    def test_handle_single_key_secret_json_dict(self):
        """Test handling single-key secret with JSON string that parses to dict."""
        nested_data = {"username": "test_user", "password": "test_pass"}
        result = SecretStore._handle_single_key_secret("key", json.dumps(nested_data))
        assert result == nested_data

    def test_handle_single_key_secret_json_empty_dict(self):
        """Test handling single-key secret with empty JSON object."""
        result = SecretStore._handle_single_key_secret("key", "{}")
        assert result == {}

    def test_handle_single_key_secret_json_array(self):
        """Test handling single-key secret with JSON array (should return {key: value})."""
        json_array = json.dumps([1, 2, 3])
        result = SecretStore._handle_single_key_secret("key", json_array)
        assert result == {"key": json_array}

    def test_handle_single_key_secret_json_string(self):
        """Test handling single-key secret with JSON string value (not dict)."""
        json_string = json.dumps("simple_string")
        result = SecretStore._handle_single_key_secret("key", json_string)
        assert result == {"key": json_string}

    def test_handle_single_key_secret_invalid_json(self):
        """Test handling single-key secret with invalid JSON string."""
        invalid_json = "not valid json {"
        result = SecretStore._handle_single_key_secret("key", invalid_json)
        assert result == {"key": invalid_json}

    def test_handle_single_key_secret_plain_string(self):
        """Test handling single-key secret with plain string value."""
        result = SecretStore._handle_single_key_secret("key", "plain_string_value")
        assert result == {"key": "plain_string_value"}

    def test_handle_single_key_secret_non_string_value(self):
        """Test handling single-key secret with non-string value."""
        result = SecretStore._handle_single_key_secret("key", 123)
        assert result == {"key": 123}

    def test_handle_single_key_secret_boolean_value(self):
        """Test handling single-key secret with boolean value."""
        result = SecretStore._handle_single_key_secret("key", True)
        assert result == {"key": True}

    def test_handle_single_key_secret_list_value(self):
        """Test handling single-key secret with list value."""
        list_value = [1, 2, 3]
        result = SecretStore._handle_single_key_secret("key", list_value)
        assert result == {"key": list_value}

    def test_handle_single_key_secret_nested_json_dict(self):
        """Test handling single-key secret with nested JSON dict."""
        nested_data = {"level1": {"level2": {"level3": "value"}, "other": "data"}}
        result = SecretStore._handle_single_key_secret("key", json.dumps(nested_data))
        assert result == nested_data

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

        result = SecretStore.apply_secret_values(source_credentials, secret_data)

        assert result["username"] == "actual_username"
        assert result["password"] == "actual_password"
        assert result["extra"]["database"] == "actual_database"

    def test_apply_secret_values_no_substitution(self):
        """Test applying secret values when no substitution is needed."""
        source_credentials = {"username": "direct_user", "password": "direct_pass"}

        secret_data = {"some_key": "some_value"}

        result = SecretStore.apply_secret_values(source_credentials, secret_data)

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

        result = SecretStore.apply_secret_values(test_credentials, safe_secret_data)

        # Verify substitutions happened correctly
        if secret_keys and "test_field" in test_credentials:
            expected_value = safe_secret_data[test_credentials["test_field"]]
            assert result["test_field"] == expected_value
            assert result["extra"]["extra_field"] == expected_value

    @patch("application_sdk.services.objectstore.DaprClient")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    @patch("application_sdk.services.secretstore.DaprClient")
    @patch("application_sdk.services.secretstore.DEPLOYMENT_NAME", "production")
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

        result = SecretStore.get_secret("test-key", component_name="test-component")

        # Verify the result includes both secret and state data
        expected_result = {
            "username": "test",
            "password": "secret",
        }
        assert result == expected_result
        mock_client.get_secret.assert_called_once_with(
            store_name="test-component", key="test-key"
        )

    @patch("application_sdk.services.objectstore.DaprClient")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    @patch("application_sdk.services.secretstore.DaprClient")
    @patch("application_sdk.services.secretstore.DEPLOYMENT_NAME", "production")
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
            SecretStore.get_secret("test-key", component_name="test-component")

    @patch("application_sdk.services.secretstore.SecretStore.get_secret")
    def test_fetch_single_key_secrets_success(self, mock_get_secret):
        """Test fetching secrets in single-key mode with successful lookups."""
        credential_config = {
            "username": "user_secret_key",
            "password": "pass_secret_key",
            "port": 5432,  # Non-string value should be skipped
        }

        mock_get_secret.side_effect = [
            {"value": "actual_username"},
            {"value": "actual_password"},
        ]

        result = SecretStore._fetch_single_key_secrets(credential_config)

        assert result == {
            "value": "actual_password"  # Last one overwrites
        }
        assert mock_get_secret.call_count == 2
        mock_get_secret.assert_any_call("user_secret_key")
        mock_get_secret.assert_any_call("pass_secret_key")

    @patch("application_sdk.services.secretstore.SecretStore.get_secret")
    def test_fetch_single_key_secrets_with_empty_values(self, mock_get_secret):
        """Test fetching secrets in single-key mode with empty values filtered out."""
        credential_config = {
            "username": "user_secret_key",
            "password": "pass_secret_key",
        }

        mock_get_secret.side_effect = [
            {"value": "actual_username", "empty": ""},
            {"value": "actual_password", "null": None},
        ]

        result = SecretStore._fetch_single_key_secrets(credential_config)

        assert "value" in result
        assert "empty" not in result
        assert "null" not in result

    @patch("application_sdk.services.secretstore.SecretStore.get_secret")
    def test_fetch_single_key_secrets_preserves_falsy_values(self, mock_get_secret):
        """Test that valid falsy values (False, 0) are preserved and not filtered out."""
        credential_config = {
            "enabled": "enabled_secret_key",
            "port": "port_secret_key",
            "count": "count_secret_key",
        }

        mock_get_secret.side_effect = [
            {"enabled_secret_key": False},  # Boolean False should be preserved
            {"port_secret_key": 0},  # Integer 0 should be preserved
            {"count_secret_key": 0.0},  # Float 0.0 should be preserved
        ]

        result = SecretStore._fetch_single_key_secrets(credential_config)

        assert result["enabled_secret_key"] is False
        assert result["port_secret_key"] == 0
        assert result["count_secret_key"] == 0.0

    @patch("application_sdk.services.secretstore.SecretStore.get_secret")
    def test_fetch_single_key_secrets_with_exceptions(self, mock_get_secret):
        """Test fetching secrets in single-key mode when some lookups fail."""
        credential_config = {
            "username": "user_secret_key",
            "password": "pass_secret_key",
            "database": "db_secret_key",
        }

        mock_get_secret.side_effect = [
            {"value": "actual_username"},
            Exception("Secret not found"),
            {"value": "actual_database"},
        ]

        result = SecretStore._fetch_single_key_secrets(credential_config)

        assert "value" in result
        assert mock_get_secret.call_count == 3

    @patch("application_sdk.services.secretstore.SecretStore.get_secret")
    def test_fetch_single_key_secrets_no_string_fields(self, mock_get_secret):
        """Test fetching secrets in single-key mode with no string fields."""
        credential_config = {
            "port": 5432,
            "enabled": True,
            "count": 100,
        }

        result = SecretStore._fetch_single_key_secrets(credential_config)

        assert result == {}
        mock_get_secret.assert_not_called()

    @pytest.mark.asyncio
    @patch("application_sdk.services.secretstore.SecretStore._fetch_single_key_secrets")
    @patch("application_sdk.services.secretstore.SecretStore.resolve_credentials")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    async def test_get_credentials_single_key_mode(
        self, mock_get_state, mock_resolve_credentials, mock_fetch_single_key
    ):
        """Test get_credentials in SINGLE_KEY mode (non-direct, no secret-path)."""
        credential_guid = "test-guid-123"
        credential_config = {
            "credentialSource": "agent",
            "username": "user_secret_key",
            "password": "pass_secret_key",
        }

        mock_get_state.return_value = credential_config
        mock_fetch_single_key.return_value = {
            "user_secret_key": "actual_username",
            "pass_secret_key": "actual_password",
        }
        mock_resolve_credentials.return_value = {
            "credentialSource": "agent",
            "username": "actual_username",
            "password": "actual_password",
        }

        result = await SecretStore.get_credentials(credential_guid)

        mock_get_state.assert_called_once_with(credential_guid, StateType.CREDENTIALS)
        mock_fetch_single_key.assert_called_once_with(credential_config)
        mock_resolve_credentials.assert_called_once()
        assert result["username"] == "actual_username"
        assert result["password"] == "actual_password"

    @pytest.mark.asyncio
    @patch("application_sdk.services.secretstore.SecretStore._fetch_single_key_secrets")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    async def test_get_credentials_single_key_mode_determination(
        self, mock_get_state, mock_fetch_single_key
    ):
        """Test that SINGLE_KEY mode is determined correctly."""
        credential_guid = "test-guid-123"

        # Test case 1: credentialSource is not "direct" and no secret-path
        credential_config = {
            "credentialSource": "agent",
            "username": "user_key",
        }
        mock_get_state.return_value = credential_config
        mock_fetch_single_key.return_value = {}
        await SecretStore.get_credentials(credential_guid)
        mock_fetch_single_key.assert_called()

        # Test case 2: credentialSource missing (defaults to "direct") should use MULTI_KEY
        mock_fetch_single_key.reset_mock()
        credential_config = {
            "username": "user_key",
        }
        mock_get_state.return_value = credential_config
        await SecretStore.get_credentials(credential_guid)
        mock_fetch_single_key.assert_not_called()

    @pytest.mark.asyncio
    @patch("application_sdk.services.secretstore.SecretStore._fetch_single_key_secrets")
    @patch("application_sdk.services.secretstore.SecretStore.resolve_credentials")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    async def test_get_credentials_single_key_mode_with_extra(
        self, mock_get_state, mock_resolve_credentials, mock_fetch_single_key
    ):
        """Test SINGLE_KEY mode with extra fields in credential config."""
        credential_guid = "test-guid-123"
        credential_config = {
            "credentialSource": "agent",
            "host": "db.example.com",
            "username": "user_secret_key",
            "extra": {"ssl_mode": "ssl_secret_key"},
        }

        mock_get_state.return_value = credential_config
        mock_fetch_single_key.return_value = {
            "user_secret_key": "actual_username",
            "ssl_secret_key": "require",
        }
        mock_resolve_credentials.return_value = {
            "credentialSource": "agent",
            "host": "db.example.com",
            "username": "actual_username",
            "extra": {"ssl_mode": "require"},
        }

        result = await SecretStore.get_credentials(credential_guid)

        mock_fetch_single_key.assert_called_once()
        assert result["username"] == "actual_username"
        assert result["extra"]["ssl_mode"] == "require"

    @pytest.mark.asyncio
    @patch("application_sdk.services.secretstore.SecretStore._fetch_single_key_secrets")
    @patch("application_sdk.services.statestore.StateStore.get_state")
    async def test_get_credentials_single_key_mode_error_handling(
        self, mock_get_state, mock_fetch_single_key
    ):
        """Test error handling in SINGLE_KEY mode."""
        credential_guid = "test-guid-123"

        mock_get_state.side_effect = Exception("State store error")

        with pytest.raises(CommonError) as exc_info:
            await SecretStore.get_credentials(credential_guid)

        assert exc_info.value.args[0] == CommonError.CREDENTIALS_RESOLUTION_ERROR
        assert "State store error" in str(exc_info.value)
        mock_fetch_single_key.assert_not_called()
