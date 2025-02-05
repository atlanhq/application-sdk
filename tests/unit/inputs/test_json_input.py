from unittest.mock import patch

import pytest

from application_sdk.inputs.json import JsonInput


@pytest.fixture
def json_input():
    return JsonInput(
        path="/test_path",
        download_file_prefix="test_prefix",
        file_names=["test_file.json"],
    )


@pytest.mark.asyncio
async def test_init(json_input):
    assert json_input.path.endswith("test_path")
    assert json_input.download_file_prefix == "test_prefix"
    assert json_input.file_names == ["test_file.json"]


@pytest.mark.asyncio
@patch("os.path.exists")
@patch(
    "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
)
async def test_not_download_file_that_exists(mock_download, mock_exists, json_input):
    mock_exists.return_value = True
    await json_input.download_files()
    mock_download.assert_not_called()


@pytest.mark.asyncio
@patch("os.path.exists")
@patch(
    "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
)
async def test_download_file(mock_download, mock_exists, json_input):
    mock_exists.return_value = False
    await json_input.download_files()
    mock_download.assert_called_once_with(
        "test_prefix/test_file.json",
        "/test_path/test_file.json",
    )
