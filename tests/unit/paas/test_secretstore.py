import json
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.paas.secretstore import SecretStore


@pytest.fixture
def mock_dapr_client():
    with patch("application_sdk.paas.secretstore.DaprClient") as mock_client:
        yield mock_client.return_value


def test_state_store_name():
    assert SecretStore.STATE_STORE_NAME == "statestore"


def test_store_credentials_success(mock_dapr_client):
    config = {"username": "test", "password": "password"}

    with patch("uuid.uuid4", return_value="test-uuid"):
        result = SecretStore.store_credentials(config)

    assert result == "credential_test-uuid"
    mock_dapr_client.save_state.assert_called_once_with(
        store_name="statestore", key="credential_test-uuid", value=json.dumps(config)
    )
    mock_dapr_client.close.assert_called_once()


def test_store_credentials_failure(mock_dapr_client):
    config = {"username": "test", "password": "password"}
    mock_dapr_client.save_state.side_effect = Exception("Dapr error")

    with pytest.raises(Exception):
        SecretStore.store_credentials(config)

    mock_dapr_client.close.assert_called_once()


def test_extract_credentials_success(mock_dapr_client):
    config = {"username": "test", "password": "password"}
    mock_state = MagicMock()
    mock_state.data = json.dumps(config)
    mock_dapr_client.get_state.return_value = mock_state

    result = SecretStore.extract_credentials("credential_test-uuid")

    assert result == config
    mock_dapr_client.get_state.assert_called_once_with(
        store_name="statestore", key="credential_test-uuid"
    )
    mock_dapr_client.close.assert_called_once()


def test_extract_credentials_invalid_guid():
    with pytest.raises(ValueError):
        SecretStore.extract_credentials("invalid-guid")


def test_extract_credentials_not_found(mock_dapr_client):
    mock_state = MagicMock()
    mock_state.data = None
    mock_dapr_client.get_state.return_value = mock_state

    with pytest.raises(ValueError):
        SecretStore.extract_credentials("credential_test-uuid")

    mock_dapr_client.close.assert_called_once()


def test_extract_credentials_failure(mock_dapr_client):
    mock_dapr_client.get_state.side_effect = Exception("Dapr error")

    with pytest.raises(Exception):
        SecretStore.extract_credentials("credential_test-uuid")

    mock_dapr_client.close.assert_called_once()
