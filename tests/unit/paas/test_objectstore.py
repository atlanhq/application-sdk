from unittest.mock import MagicMock, patch

import pytest

from application_sdk.paas.objectstore import ObjectStore


@pytest.fixture
def mock_dapr_client():
    with patch("application_sdk.paas.objectstore.DaprClient") as mock_client:
        yield mock_client.return_value


def test_object_store_constants():
    assert ObjectStore.OBJECT_STORE_NAME == "objectstore"
    assert ObjectStore.OBJECT_CREATE_OPERATION == "create"


def test_push_to_object_store_invalid_path():
    with pytest.raises(ValueError):
        ObjectStore.push_to_object_store("/invalid/path", "/invalid/path")


@patch("os.path.isdir", return_value=True)
@patch("os.walk")
def test_push_to_object_store_success(mock_walk, mock_isdir, mock_dapr_client):
    mock_walk.return_value = [
        ("/test/path", [], ["file1.txt", "file2.txt"]),
    ]

    with patch("builtins.open", MagicMock()):
        ObjectStore.push_to_object_store("/test", "/test/path")

    assert mock_dapr_client.invoke_binding.call_count == 2
    mock_dapr_client.close.assert_called_once()


@patch("os.path.isdir", return_value=True)
@patch("os.walk")
def test_push_to_object_store_file_read_error(mock_walk, mock_isdir, mock_dapr_client):
    mock_walk.return_value = [
        ("/test/path", [], ["file1.txt"]),
    ]

    with patch("builtins.open", side_effect=IOError):
        ObjectStore.push_to_object_store("/test", "/test/path")

    mock_dapr_client.invoke_binding.assert_not_called()
    mock_dapr_client.close.assert_called_once()


@patch("os.path.isdir", return_value=True)
@patch("os.walk")
def test_push_to_object_store_dapr_error(mock_walk, mock_isdir, mock_dapr_client):
    mock_walk.return_value = [
        ("/test/path", [], ["file1.txt"]),
    ]
    mock_dapr_client.invoke_binding.side_effect = Exception("Dapr error")

    with patch("builtins.open", MagicMock()):
        with pytest.raises(Exception):
            ObjectStore.push_to_object_store("/test", "/test/path")

    mock_dapr_client.close.assert_called_once()
