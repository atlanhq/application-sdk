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
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_files_internal_success(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test successful internal file migration."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        files_to_migrate = ["file1.txt", "file2.txt", "file3.txt"]
        file_data = b"test content"
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput._migrate_files_internal(
            files_to_migrate, "test_prefix"
        )

        # Assert
        assert result["total_files"] == 3
        assert result["migrated_files"] == 3
        assert result["failed_migrations"] == 0
        assert result["prefix"] == "test_prefix"
        assert result["source"] == "objectstore"
        assert result["destination"] == AtlanStorageOutput.ATLAN_STORE_NAME
        assert len(result["failures"]) == 0

        # Verify each file was processed
        assert mock_objectstore_input.get_file_data.call_count == 3
        assert mock_client.invoke_binding.call_count == 3

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_files_internal_with_failures(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test internal file migration with some failures."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        files_to_migrate = ["file1.txt", "file2.txt", "file3.txt"]
        file_data = b"test content"
        mock_objectstore_input.get_file_data.side_effect = [
            file_data,  # file1.txt - success
            Exception("File not found"),  # file2.txt - failure
            file_data,  # file3.txt - success
        ]

        # Act
        result = await AtlanStorageOutput._migrate_files_internal(
            files_to_migrate, "test_prefix"
        )

        # Assert
        assert result["total_files"] == 3
        assert result["migrated_files"] == 2
        assert result["failed_migrations"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["file"] == "file2.txt"
        assert "File not found" in result["failures"][0]["error"]

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_files_internal_empty_list(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test internal file migration with empty file list."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        files_to_migrate = []

        # Act
        result = await AtlanStorageOutput._migrate_files_internal(
            files_to_migrate, "test_prefix"
        )

        # Assert
        assert result["total_files"] == 0
        assert result["migrated_files"] == 0
        assert result["failed_migrations"] == 0
        assert result["prefix"] == "test_prefix"
        assert len(result["failures"]) == 0

        # Verify no calls were made
        mock_objectstore_input.get_file_data.assert_not_called()
        mock_client.invoke_binding.assert_not_called()

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_from_objectstore_success(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test successful migration from objectstore."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        files_to_migrate = ["file1.txt", "file2.txt"]
        file_data = b"test content"
        mock_objectstore_input.list_all_files.return_value = files_to_migrate
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore("test_prefix")

        # Assert
        mock_objectstore_input.list_all_files.assert_called_once_with("test_prefix")
        assert result["total_files"] == 2
        assert result["migrated_files"] == 2
        assert result["failed_migrations"] == 0
        assert result["prefix"] == "test_prefix"

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    async def test_migrate_from_objectstore_list_error(
        self, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration error when listing files fails."""
        # Setup
        mock_objectstore_input.list_all_files.side_effect = Exception("List failed")

        # Act & Assert
        with pytest.raises(Exception, match="List failed"):
            await AtlanStorageOutput.migrate_from_objectstore("test_prefix")

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    def test_migrate_specific_files_success(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test successful migration of specific files."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        file_paths = ["file1.txt", "file2.txt"]
        file_data = b"test content"
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = AtlanStorageOutput.migrate_specific_files(file_paths)

        # Assert
        assert result["total_files"] == 2
        assert result["migrated_files"] == 2
        assert result["failed_migrations"] == 0
        assert len(result["failures"]) == 0

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    def test_migrate_specific_files_error(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration error when getting file data fails."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        file_paths = ["file1.txt"]
        mock_objectstore_input.get_file_data.side_effect = Exception("Get data failed")

        # Act
        result = AtlanStorageOutput.migrate_specific_files(file_paths)

        # Assert
        assert result["total_files"] == 1
        assert result["migrated_files"] == 0
        assert result["failed_migrations"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["file"] == "file1.txt"
        assert "Get data failed" in result["failures"][0]["error"]

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_prefix_batch_success(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test successful batch migration of multiple prefixes."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        prefixes = ["prefix1", "prefix2"]
        files_per_prefix = ["file1.txt", "file2.txt"]
        file_data = b"test content"

        mock_objectstore_input.list_all_files.side_effect = [
            files_per_prefix,
            files_per_prefix,
        ]
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput.migrate_prefix_batch(prefixes)

        # Assert
        assert result["total_prefixes"] == 2
        assert result["total_files"] == 4
        assert result["migrated_files"] == 4
        assert result["failed_migrations"] == 0
        assert len(result["failures"]) == 0
        assert result["source"] == "objectstore"
        assert result["destination"] == AtlanStorageOutput.ATLAN_STORE_NAME

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_prefix_batch_with_failures(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test batch migration with some prefix failures."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        prefixes = ["prefix1", "prefix2", "prefix3"]
        files_per_prefix = ["file1.txt", "file2.txt"]
        file_data = b"test content"

        # prefix1: success, prefix2: failure, prefix3: success
        mock_objectstore_input.list_all_files.side_effect = [
            files_per_prefix,  # prefix1 - success
            Exception("Prefix2 failed"),  # prefix2 - failure
            files_per_prefix,  # prefix3 - success
        ]
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput.migrate_prefix_batch(prefixes)

        # Assert
        assert result["total_prefixes"] == 3
        assert result["total_files"] == 4  # 2 from prefix1 + 2 from prefix3
        assert result["migrated_files"] == 4
        assert result["failed_migrations"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["prefix"] == "prefix2"
        assert "Prefix2 failed" in result["failures"][0]["error"]

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    async def test_migrate_prefix_batch_empty_list(
        self, mock_objectstore_input: MagicMock
    ) -> None:
        """Test batch migration with empty prefix list."""
        # Setup
        prefixes = []

        # Act
        result = await AtlanStorageOutput.migrate_prefix_batch(prefixes)

        # Assert
        assert result["total_prefixes"] == 0
        assert result["total_files"] == 0
        assert result["migrated_files"] == 0
        assert result["failed_migrations"] == 0
        assert len(result["failures"]) == 0

        # Verify no calls were made
        mock_objectstore_input.list_all_files.assert_not_called()

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_files_internal_large_batch(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test internal migration with large batch to verify progress logging."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        # Create 150 files to test progress logging (every 100 files)
        files_to_migrate = [f"file{i}.txt" for i in range(1, 151)]
        file_data = b"test content"
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput._migrate_files_internal(
            files_to_migrate, "test_prefix"
        )

        # Assert
        assert result["total_files"] == 150
        assert result["migrated_files"] == 150
        assert result["failed_migrations"] == 0
        assert mock_objectstore_input.get_file_data.call_count == 150
        assert mock_client.invoke_binding.call_count == 150

    @patch("application_sdk.outputs.atlanstorage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlanstorage.DaprClient")
    async def test_migrate_files_internal_upload_error(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test internal migration when upload fails."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client
        mock_client.invoke_binding.side_effect = Exception("Upload failed")

        files_to_migrate = ["file1.txt", "file2.txt"]
        file_data = b"test content"
        mock_objectstore_input.get_file_data.return_value = file_data

        # Act
        result = await AtlanStorageOutput._migrate_files_internal(
            files_to_migrate, "test_prefix"
        )

        # Assert
        assert result["total_files"] == 2
        assert result["migrated_files"] == 0
        assert result["failed_migrations"] == 2
        assert len(result["failures"]) == 2
        assert all(
            "Upload failed" in failure["error"] for failure in result["failures"]
        )

    def test_class_constants(self) -> None:
        """Test that class constants are correctly defined."""
        assert AtlanStorageOutput.ATLAN_STORE_NAME == "atlan-storage"
        assert AtlanStorageOutput.OBJECT_CREATE_OPERATION == "create"
