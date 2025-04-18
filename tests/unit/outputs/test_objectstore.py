"""Unit tests for object store input and output operations."""

from unittest.mock import MagicMock, mock_open, patch

import pytest

from application_sdk.constants import OBJECT_STORE_NAME
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.outputs.objectstore import ObjectStoreOutput


@pytest.mark.asyncio
class TestObjectStoreOutput:
    @patch("application_sdk.outputs.objectstore.DaprClient")
    async def test_push_file_to_object_store_success(
        self, mock_dapr_client: MagicMock
    ) -> None:
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        test_file_content = b"test content"
        mock_file = mock_open(read_data=test_file_content)

        with patch("builtins.open", mock_file):
            await ObjectStoreOutput.push_file_to_object_store(
                output_prefix="/test/prefix", file_path="/test/prefix/test.txt"
            )

        # Assertions
        mock_client.invoke_binding.assert_called_once_with(
            binding_name=OBJECT_STORE_NAME,
            operation=ObjectStoreOutput.OBJECT_CREATE_OPERATION,
            data=test_file_content,
            binding_metadata={
                "key": "test.txt",
                "blobName": "test.txt",
                "fileName": "test.txt",
            },
        )

    @patch("application_sdk.outputs.objectstore.DaprClient")
    async def test_push_file_to_object_store_file_error(
        self, mock_dapr_client: MagicMock
    ) -> None:
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        mock_file = mock_open()
        mock_file.side_effect = IOError("Test file error")

        with patch("builtins.open", mock_file):
            with pytest.raises(IOError, match="Test file error"):
                await ObjectStoreOutput.push_file_to_object_store(
                    output_prefix="/test/prefix", file_path="/test/prefix/test.txt"
                )

        # Assert that invoke_binding was not called
        mock_client.invoke_binding.assert_not_called()

    @patch("application_sdk.outputs.objectstore.DaprClient")
    async def test_push_files_to_object_store_success(
        self, mock_dapr_client: MagicMock
    ) -> None:
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        test_files = {"file1.txt": b"content1", "file2.txt": b"content2"}

        with patch("os.walk") as mock_walk, patch("os.path.isdir") as mock_isdir, patch(
            "builtins.open", mock_open()
        ):
            mock_isdir.return_value = True
            mock_walk.return_value = [("/test/input", [], list(test_files.keys()))]

            await ObjectStoreOutput.push_files_to_object_store(
                output_prefix="/test/prefix", input_files_path="/test/input"
            )

        # Assert invoke_binding was called for each file
        assert mock_client.invoke_binding.call_count == len(test_files)

    @patch("application_sdk.outputs.objectstore.DaprClient")
    async def test_push_files_to_object_store_invalid_directory(
        self, mock_dapr_client: MagicMock
    ) -> None:
        with patch("os.path.isdir") as mock_isdir:
            mock_isdir.return_value = False

            with pytest.raises(ValueError, match="not a valid directory"):
                await ObjectStoreOutput.push_files_to_object_store(
                    output_prefix="/test/prefix", input_files_path="/invalid/path"
                )


class TestObjectStoreInput:
    @patch("application_sdk.inputs.objectstore.DaprClient")
    def test_download_file_from_object_store_success(
        self, mock_dapr_client: MagicMock
    ) -> None:
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.data = b"test content"
        mock_client.invoke_binding.return_value = mock_response

        mock_file = mock_open()

        with patch("builtins.open", mock_file):
            ObjectStoreInput.download_file_from_object_store(
                download_file_prefix="/test/prefix", file_path="/test/prefix/test.txt"
            )

        # Assertions
        mock_client.invoke_binding.assert_called_once_with(
            binding_name=OBJECT_STORE_NAME,
            operation=ObjectStoreInput.OBJECT_GET_OPERATION,
            binding_metadata={"key": "test.txt", "fileName": "test.txt"},
        )

        mock_file().write.assert_called_once_with(b"test content")

    @patch("application_sdk.inputs.objectstore.DaprClient")
    def test_download_file_from_object_store_error(
        self, mock_dapr_client: MagicMock
    ) -> None:
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        mock_client.invoke_binding.side_effect = Exception("Test download error")

        with pytest.raises(Exception, match="Test download error"):
            ObjectStoreInput.download_file_from_object_store(
                download_file_prefix="/test/prefix", file_path="/test/prefix/test.txt"
            )
