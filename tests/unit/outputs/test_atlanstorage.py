"""Unit tests for Atlan storage output operations."""

from unittest.mock import MagicMock, patch

import pytest

from application_sdk.outputs.atlanstorage import AtlanStorageOutput


class TestAtlanStorageOutput:
    """Test suite for AtlanStorageOutput."""

    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    def test_upload_file_success(self, mock_dapr_client: MagicMock) -> None:
        """Test successful file upload to Atlan storage."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        file_path = "test/path/file.txt"
        file_data = b"test file content"

        # Act
        AtlanStorageOutput.upload_file(file_path, file_data)

        # Assert
        mock_client.invoke_binding.assert_called_once_with(
            binding_name=AtlanStorageOutput.ATLAN_STORE_NAME,
            operation=AtlanStorageOutput.OBJECT_CREATE_OPERATION,
            data=file_data,
            binding_metadata={"key": file_path, "fileName": file_path},
        )

    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    def test_upload_file_error(self, mock_dapr_client: MagicMock) -> None:
        """Test file upload error handling."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client
        mock_client.invoke_binding.side_effect = Exception("Upload failed")

        file_path = "test/path/file.txt"
        file_data = b"test file content"

        # Act & Assert
        with pytest.raises(Exception, match="Upload failed"):
            AtlanStorageOutput.upload_file(file_path, file_data)

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.AtlanStorageOutput.upload_file")
    async def test_migrate_from_objectstore_to_atlan_success(
        self, mock_upload_file: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test successful migration from objectstore to Atlan storage."""
        # Setup
        files_to_migrate = ["file1.txt", "file2.txt"]
        file_data = b"test content"
        mock_objectstore_input.list_all_files.return_value = files_to_migrate
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        mock_objectstore_input.list_all_files.assert_called_once_with("test_prefix")
        assert mock_objectstore_input.get_file_data.call_count == 2
        assert mock_upload_file.call_count == 2
        assert result["total_files"] == 2
        assert result["migrated_files"] == 2
        assert result["failed_migrations"] == 0
        assert result["prefix"] == "test_prefix"
        assert result["source"] == "objectstore"
        assert result["destination"] == AtlanStorageOutput.ATLAN_STORE_NAME

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    async def test_migrate_from_objectstore_to_atlan_empty_list(
        self, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration when no files are found."""
        # Setup
        mock_objectstore_input.list_all_files.return_value = []

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        mock_objectstore_input.list_all_files.assert_called_once_with("test_prefix")
        mock_objectstore_input.get_file_data.assert_not_called()
        assert result["total_files"] == 0
        assert result["migrated_files"] == 0
        assert result["failed_migrations"] == 0
        assert result["prefix"] == "test_prefix"

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.AtlanStorageOutput.upload_file")
    async def test_migrate_from_objectstore_to_atlan_with_failures(
        self, mock_upload_file: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration with some file failures."""
        # Setup
        files_to_migrate = ["file1.txt", "file2.txt", "file3.txt"]
        file_data = b"test content"
        mock_objectstore_input.list_all_files.return_value = files_to_migrate
        mock_objectstore_input.get_file_data.return_value = file_data
        mock_upload_file.side_effect = [None, Exception("Upload failed"), None]

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        assert result["total_files"] == 3
        assert result["migrated_files"] == 2
        assert result["failed_migrations"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["file"] == "file2.txt"
        assert "Upload failed" in result["failures"][0]["error"]

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    async def test_migrate_from_objectstore_to_atlan_list_error(
        self, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration error when listing files fails."""
        # Setup
        mock_objectstore_input.list_all_files.side_effect = Exception("List failed")

        # Act & Assert
        with pytest.raises(Exception, match="List failed"):
            await AtlanStorageOutput.migrate_from_objectstore_to_atlan("test_prefix")

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.AtlanStorageOutput.upload_file")
    async def test_migrate_from_objectstore_to_atlan_get_data_error(
        self, mock_upload_file: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration when getting file data fails."""
        # Setup
        files_to_migrate = ["file1.txt"]
        mock_objectstore_input.list_all_files.return_value = files_to_migrate
        mock_objectstore_input.get_file_data.side_effect = Exception("Get data failed")

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        assert result["total_files"] == 1
        assert result["migrated_files"] == 0
        assert result["failed_migrations"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["file"] == "file1.txt"
        assert "Get data failed" in result["failures"][0]["error"]

    def test_class_constants(self) -> None:
        """Test that class constants are correctly defined."""
        assert AtlanStorageOutput.ATLAN_STORE_NAME == "atlan-storage"
        assert AtlanStorageOutput.OBJECT_CREATE_OPERATION == "create"
