"""Unit tests for Atlan storage output operations."""

from unittest.mock import MagicMock, patch

import pytest

from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.outputs.atlan_storage import AtlanStorageOutput


class TestAtlanStorageOutput:
    """Test suite for AtlanStorageOutput."""

    @patch("application_sdk.outputs.atlan_storage.DaprClient")
    async def test_migrate_single_file_success(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test successful single file migration to Atlan storage."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        file_path = "test/path/file.txt"
        file_data = b"test file content"

        with patch(
            "application_sdk.outputs.atlan_storage.ObjectStoreInput.get_file_data",
            return_value=file_data,
        ):
            # Act
            result = await AtlanStorageOutput._migrate_single_file(file_path)

            # Assert
            file_path_result, success, error_msg = result
            assert file_path_result == file_path
            assert success is True
            assert error_msg == ""

            mock_client.invoke_binding.assert_called_once_with(
                binding_name=UPSTREAM_OBJECT_STORE_NAME,
                operation=AtlanStorageOutput.OBJECT_CREATE_OPERATION,
                data=file_data,
                binding_metadata={"key": file_path},
            )

    @patch("application_sdk.outputs.atlan_storage.DaprClient")
    async def test_migrate_single_file_error(self, mock_dapr_client: MagicMock) -> None:
        """Test single file migration error handling."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client
        mock_client.invoke_binding.side_effect = Exception("Upload failed")

        file_path = "test/path/file.txt"
        file_data = b"test file content"

        with patch(
            "application_sdk.outputs.atlan_storage.ObjectStoreInput.get_file_data",
            return_value=file_data,
        ):
            # Act
            result = await AtlanStorageOutput._migrate_single_file(file_path)

            # Assert
            file_path_result, success, error_msg = result
            assert file_path_result == file_path
            assert success is False
            assert "Upload failed" in error_msg

    @patch("application_sdk.outputs.atlan_storage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlan_storage.DaprClient")
    async def test_migrate_from_objectstore_to_atlan_success(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test successful migration from objectstore to Atlan storage."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        mock_objectstore_input.list_all_files.return_value = [
            "file1.json",
            "file2.json",
            "file3.json",
        ]
        mock_objectstore_input.get_file_data.side_effect = [
            b"file1 content",
            b"file2 content",
            b"file3 content",
        ]

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        assert result.total_files == 3
        assert result.migrated_files == 3
        assert result.failed_migrations == 0
        assert result.prefix == "test_prefix"
        # UPSTREAM_OBJECT_STORE_NAME constant value
        assert len(result.failures) == 0

        # Verify DaprClient was called for each file
        assert mock_client.invoke_binding.call_count == 3
        mock_client.invoke_binding.assert_any_call(
            binding_name=UPSTREAM_OBJECT_STORE_NAME,
            operation=AtlanStorageOutput.OBJECT_CREATE_OPERATION,
            data=b"file1 content",
            binding_metadata={"key": "file1.json"},
        )
        mock_client.invoke_binding.assert_any_call(
            binding_name=UPSTREAM_OBJECT_STORE_NAME,
            operation=AtlanStorageOutput.OBJECT_CREATE_OPERATION,
            data=b"file2 content",
            binding_metadata={"key": "file2.json"},
        )
        mock_client.invoke_binding.assert_any_call(
            binding_name=UPSTREAM_OBJECT_STORE_NAME,
            operation=AtlanStorageOutput.OBJECT_CREATE_OPERATION,
            data=b"file3 content",
            binding_metadata={"key": "file3.json"},
        )

    @patch("application_sdk.outputs.atlan_storage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlan_storage.DaprClient")
    async def test_migrate_from_objectstore_to_atlan_with_failures(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration with some failures."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        mock_objectstore_input.list_all_files.return_value = [
            "file1.json",
            "file2.json",
            "file3.json",
        ]
        mock_objectstore_input.get_file_data.side_effect = [
            b"file1 content",
            Exception("File 2 error"),
            b"file3 content",
        ]
        # file1 will succeed, file2 will fail due to get_file_data exception, file3 will succeed
        mock_client.invoke_binding.side_effect = [
            None,  # file1 succeeds
            None,  # file3 succeeds (file2 never gets here due to get_file_data exception)
        ]

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        assert result.total_files == 3
        assert result.migrated_files == 2  # file1 and file3 succeeded
        assert result.failed_migrations == 1  # file2 failed
        assert len(result.failures) == 1

        # Check failure details
        failure_files = [f["file"] for f in result.failures]
        assert "file2.json" in failure_files
        assert "File 2 error" in result.failures[0]["error"]

    @patch("application_sdk.outputs.atlan_storage.ObjectStoreInput")
    async def test_migrate_from_objectstore_to_atlan_empty_list(
        self, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration with no files to migrate."""
        # Setup
        mock_objectstore_input.list_all_files.return_value = []

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        assert result.total_files == 0
        assert result.migrated_files == 0
        assert result.failed_migrations == 0
        assert result.prefix == "test_prefix"
        assert len(result.failures) == 0

    @patch("application_sdk.outputs.atlan_storage.ObjectStoreInput")
    async def test_migrate_from_objectstore_to_atlan_list_error(
        self, mock_objectstore_input: MagicMock
    ) -> None:
        """Test migration when listing files fails."""
        # Setup
        mock_objectstore_input.list_all_files.side_effect = Exception("List error")

        # Act & Assert
        with pytest.raises(Exception, match="List error"):
            await AtlanStorageOutput.migrate_from_objectstore_to_atlan("test_prefix")

    @patch("application_sdk.outputs.atlan_storage.ObjectStoreInput")
    async def test_migrate_from_objectstore_to_atlan_get_data_error(
        self, mock_objectstore_input: MagicMock
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
        assert result.total_files == 1
        assert result.migrated_files == 0
        assert result.failed_migrations == 1
        assert len(result.failures) == 1
        assert result.failures[0]["file"] == "file1.txt"
        assert "Get data failed" in result.failures[0]["error"]

    @patch("application_sdk.outputs.atlan_storage.ObjectStoreInput")
    @patch("application_sdk.outputs.atlan_storage.DaprClient")
    async def test_parallel_file_migration(
        self, mock_dapr_client: MagicMock, mock_objectstore_input: MagicMock
    ) -> None:
        """Test that files are migrated in parallel."""
        # Setup
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        files = [f"file{i}.json" for i in range(10)]
        mock_objectstore_input.list_all_files.return_value = files
        mock_objectstore_input.get_file_data.side_effect = [b"content"] * 10

        # Act
        result = await AtlanStorageOutput.migrate_from_objectstore_to_atlan(
            "test_prefix"
        )

        # Assert
        assert result.total_files == 10
        assert result.migrated_files == 10
        assert mock_client.invoke_binding.call_count == 10

        # Verify all files were processed
        for file_path in files:
            mock_client.invoke_binding.assert_any_call(
                binding_name=UPSTREAM_OBJECT_STORE_NAME,
                operation=AtlanStorageOutput.OBJECT_CREATE_OPERATION,
                data=b"content",
                binding_metadata={"key": file_path},
            )
