import uuid
from unittest.mock import MagicMock, call, patch

import pytest

from sdk.const import OBJECT_CREATE_OPERATION, OBJECT_STORE_NAME, STATE_STORE_NAME
from sdk.dto.credentials import BasicCredential
from sdk.interfaces.platform import Platform


@pytest.fixture
def mock_dapr_client():
    """
    Fixture to mock the DaprClient for testing.

    Returns:
        MagicMock: A mocked instance of DaprClient.
    """
    with patch("sdk.interfaces.platform.DaprClient") as mock:
        yield mock.return_value


def test_store_credentials(mock_dapr_client):
    """
    Test the store_credentials method of the Platform class.

    This test verifies that:
    1. The method returns the correct credential GUID.
    2. The save_state method of DaprClient is called with the correct arguments.

    Args:
        mock_dapr_client (MagicMock): A mocked instance of DaprClient.
    """
    config = BasicCredential(
        user="test_user",
        password="test_pass",
        host="test_host",
        port=1234,
        database="test_database",
    )

    with patch(
        "uuid.uuid4", return_value=uuid.UUID("12345678-1234-5678-1234-567812345678")
    ):
        credential_guid = Platform.store_credentials(config)

    assert credential_guid == "credential_12345678-1234-5678-1234-567812345678"
    mock_dapr_client.save_state.assert_called_once_with(
        store_name=STATE_STORE_NAME,
        key="credential_12345678-1234-5678-1234-567812345678",
        value=config.model_dump_json(),
    )


def test_extract_credentials(mock_dapr_client):
    """
    Test the extract_credentials method of the Platform class.

    This test verifies that:
    1. The method correctly extracts and returns a BasicCredential object.
    2. The get_state method of DaprClient is called with the correct arguments.

    Args:
        mock_dapr_client (MagicMock): A mocked instance of DaprClient.
    """
    mock_state = MagicMock()
    mock_state.data = '{"host": "test_host", "port": 1234, "user": "test_user", "password": "test_pass", "database": "test_database"}'
    mock_dapr_client.get_state.return_value = mock_state

    credential = Platform.extract_credentials("credential_guid")

    assert isinstance(credential, BasicCredential)
    assert credential.user == "test_user"
    assert credential.password == "test_pass"
    assert credential.host == "test_host"
    assert credential.port == 1234
    assert credential.database == "test_database"

    mock_dapr_client.get_state.assert_called_once_with(
        store_name=STATE_STORE_NAME, key="credential_guid"
    )


def test_push_to_object_store(mock_dapr_client):
    """
    Test the push_to_object_store method of the Platform class.

    This test verifies that:
    1. The method correctly walks through the directory and pushes files to the object store.
    2. The invoke_binding method of DaprClient is called the correct number of times with the right arguments.

    Args:
        mock_dapr_client (MagicMock): A mocked instance of DaprClient.
    """
    with patch("os.path.isdir", return_value=True), patch(
        "os.walk"
    ) as mock_walk, patch("builtins.open", MagicMock()):
        mock_walk.return_value = [
            ("/tmp/metadata/workflowId/runId", [], ["file1.txt", "file2.txt"]),
        ]

        mock_file = MagicMock()
        mock_file.__enter__.return_value.read.return_value = b"test content"

        with patch("builtins.open", return_value=mock_file):
            Platform.push_to_object_store(
                "/tmp/metadata", "/tmp/metadata/workflowId/runId"
            )

        assert mock_dapr_client.invoke_binding.call_count == 2

        expected_calls = [
            call(
                binding_name=OBJECT_STORE_NAME,
                operation=OBJECT_CREATE_OPERATION,
                data=b"test content",
                binding_metadata={
                    "key": "workflowId/runId/file1.txt",
                    "fileName": "workflowId/runId/file1.txt",
                },
            ),
            call(
                binding_name=OBJECT_STORE_NAME,
                operation=OBJECT_CREATE_OPERATION,
                data=b"test content",
                binding_metadata={
                    "key": "workflowId/runId/file2.txt",
                    "fileName": "workflowId/runId/file2.txt",
                },
            ),
        ]

        mock_dapr_client.invoke_binding.assert_has_calls(expected_calls, any_order=True)
