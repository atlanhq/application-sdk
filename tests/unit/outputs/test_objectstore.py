"""Unit tests for object store input and output operations."""

import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME
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

        with patch("builtins.open", mock_file), patch(
            "application_sdk.outputs.objectstore.ObjectStoreOutput._cleanup_local_path"
        ) as mock_cleanup:
            await ObjectStoreOutput.push_file_to_object_store(
                output_prefix="/test/prefix", file_path="/test/prefix/test.txt"
            )

        # Assertions
        mock_client.invoke_binding.assert_called_once_with(
            binding_name=DEPLOYMENT_OBJECT_STORE_NAME,
            operation=ObjectStoreOutput.OBJECT_CREATE_OPERATION,
            data=test_file_content,
            binding_metadata={
                "key": "test.txt",
                "blobName": "test.txt",
                "fileName": "test.txt",
            },
        )
        mock_cleanup.assert_called_once_with("/test/prefix/test.txt")

    @patch("application_sdk.outputs.objectstore.DaprClient")
    async def test_push_file_to_object_store_file_error(
        self, mock_dapr_client: MagicMock
    ) -> None:
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        mock_file = mock_open()
        mock_file.side_effect = IOError("Test file error")

        with patch("builtins.open", mock_file), patch(
            "application_sdk.outputs.objectstore.ObjectStoreOutput._cleanup_local_path"
        ) as mock_cleanup:
            with pytest.raises(IOError, match="Test file error"):
                await ObjectStoreOutput.push_file_to_object_store(
                    output_prefix="/test/prefix", file_path="/test/prefix/test.txt"
                )

        # Assert that invoke_binding was not called
        mock_client.invoke_binding.assert_not_called()
        # Cleanup is not reached when file read fails
        mock_cleanup.assert_not_called()

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
        ), patch(
            "application_sdk.outputs.objectstore.ObjectStoreOutput._cleanup_local_path"
        ) as mock_cleanup:
            mock_isdir.return_value = True
            mock_walk.return_value = [("/test/input", [], list(test_files.keys()))]

            await ObjectStoreOutput.push_files_to_object_store(
                output_prefix="/test/prefix", input_files_path="/test/input"
            )

        # Assert invoke_binding was called for each file
        assert mock_client.invoke_binding.call_count == len(test_files)
        assert mock_cleanup.call_count == len(test_files)
        called_paths = {
            os.path.normpath(args[0][0]) for args in mock_cleanup.call_args_list
        }
        expected_paths = {
            os.path.normpath(os.path.join("/test/input", "file1.txt")),
            os.path.normpath(os.path.join("/test/input", "file2.txt")),
        }
        assert expected_paths.issubset(called_paths)

    @patch("application_sdk.outputs.objectstore.DaprClient")
    async def test_push_files_to_object_store_invalid_directory(
        self, mock_dapr_client: MagicMock
    ) -> None:
        with patch("os.path.isdir") as mock_isdir, patch(
            "application_sdk.outputs.objectstore.ObjectStoreOutput._cleanup_local_path"
        ) as mock_cleanup:
            mock_isdir.return_value = False

            with pytest.raises(ValueError, match="not a valid directory"):
                await ObjectStoreOutput.push_files_to_object_store(
                    output_prefix="/test/prefix", input_files_path="/invalid/path"
                )
            mock_cleanup.assert_not_called()


class TestObjectStoreInput:
    @patch("application_sdk.inputs.objectstore.ObjectStoreInput.get_file_data")
    @patch("os.makedirs")
    def test_download_file_from_object_store_success(
        self, mock_makedirs: MagicMock, mock_get_file_data: MagicMock
    ) -> None:
        # Setup
        mock_get_file_data.return_value = b"test content"

        mock_file = mock_open()

        with patch("builtins.open", mock_file):
            ObjectStoreInput.download_file_from_object_store(
                download_file_prefix="/test/prefix", file_path="/test/prefix/test.txt"
            )

        # Assertions
        mock_get_file_data.assert_called_once_with(
            "test.txt", DEPLOYMENT_OBJECT_STORE_NAME
        )
        mock_file().write.assert_called_once_with(b"test content")

    @patch("application_sdk.inputs.objectstore.ObjectStoreInput.get_file_data")
    @patch("os.makedirs")
    def test_download_file_from_object_store_error(
        self, mock_makedirs: MagicMock, mock_get_file_data: MagicMock
    ) -> None:
        # Setup
        mock_get_file_data.side_effect = Exception("Test download error")

        with pytest.raises(Exception, match="Test download error"):
            ObjectStoreInput.download_file_from_object_store(
                download_file_prefix="/test/prefix", file_path="/test/prefix/test.txt"
            )
