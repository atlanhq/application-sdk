"""Unit tests for Atlan storage output operations."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.services.atlan_storage import AtlanStorage


class TestAtlanStorage:
    """Test suite for AtlanStorage."""

    def setup_method(self):
        """Reset cached stores before each test."""
        AtlanStorage._deployment_store = None
        AtlanStorage._upstream_store = None

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch("application_sdk.services.atlan_storage.upload_file", new_callable=AsyncMock)
    @patch(
        "application_sdk.services.atlan_storage.download_file", new_callable=AsyncMock
    )
    async def test_migrate_single_file_success(
        self,
        mock_download: AsyncMock,
        mock_upload: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test successful single file migration to Atlan storage."""
        mock_create_store.return_value = MagicMock()
        file_path = "test/path/file.txt"

        result = await AtlanStorage._migrate_single_file(file_path)

        file_path_result, success, error_msg = result
        assert file_path_result == file_path
        assert success is True
        assert error_msg == ""
        mock_download.assert_called_once()
        mock_upload.assert_called_once()

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch(
        "application_sdk.services.atlan_storage.upload_file",
        new_callable=AsyncMock,
        side_effect=Exception("Upload failed"),
    )
    @patch(
        "application_sdk.services.atlan_storage.download_file", new_callable=AsyncMock
    )
    async def test_migrate_single_file_error(
        self,
        mock_download: AsyncMock,
        mock_upload: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test single file migration error handling."""
        mock_create_store.return_value = MagicMock()
        file_path = "test/path/file.txt"

        result = await AtlanStorage._migrate_single_file(file_path)

        file_path_result, success, error_msg = result
        assert file_path_result == file_path
        assert success is False
        assert "Upload failed" in error_msg

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch("application_sdk.services.atlan_storage.upload_file", new_callable=AsyncMock)
    @patch(
        "application_sdk.services.atlan_storage.download_file", new_callable=AsyncMock
    )
    @patch(
        "application_sdk.services.atlan_storage.list_keys",
        new_callable=AsyncMock,
        return_value=["file1.json", "file2.json", "file3.json"],
    )
    async def test_migrate_from_objectstore_to_atlan_success(
        self,
        mock_list_keys: AsyncMock,
        mock_download: AsyncMock,
        mock_upload: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test successful migration from objectstore to Atlan storage."""
        mock_create_store.return_value = MagicMock()

        result = await AtlanStorage.migrate_from_objectstore_to_atlan("test_prefix")

        assert result.total_files == 3
        assert result.migrated_files == 3
        assert result.failed_migrations == 0
        assert result.prefix == "test_prefix"
        assert len(result.failures) == 0
        assert mock_upload.call_count == 3

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch(
        "application_sdk.services.atlan_storage.upload_file",
        new_callable=AsyncMock,
        side_effect=[None, Exception("File 2 error"), None],
    )
    @patch(
        "application_sdk.services.atlan_storage.download_file", new_callable=AsyncMock
    )
    @patch(
        "application_sdk.services.atlan_storage.list_keys",
        new_callable=AsyncMock,
        return_value=["file1.json", "file2.json", "file3.json"],
    )
    async def test_migrate_from_objectstore_to_atlan_with_failures(
        self,
        mock_list_keys: AsyncMock,
        mock_download: AsyncMock,
        mock_upload: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test migration with some failures."""
        mock_create_store.return_value = MagicMock()

        result = await AtlanStorage.migrate_from_objectstore_to_atlan("test_prefix")

        assert result.total_files == 3
        assert result.migrated_files == 2
        assert result.failed_migrations == 1
        assert len(result.failures) == 1
        failure_files = [f["file"] for f in result.failures]
        assert "file2.json" in failure_files

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch(
        "application_sdk.services.atlan_storage.list_keys",
        new_callable=AsyncMock,
        return_value=[],
    )
    async def test_migrate_from_objectstore_to_atlan_empty_list(
        self,
        mock_list_keys: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test migration with no files to migrate."""
        mock_create_store.return_value = MagicMock()

        result = await AtlanStorage.migrate_from_objectstore_to_atlan("test_prefix")

        assert result.total_files == 0
        assert result.migrated_files == 0
        assert result.failed_migrations == 0
        assert result.prefix == "test_prefix"
        assert len(result.failures) == 0

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch(
        "application_sdk.services.atlan_storage.list_keys",
        new_callable=AsyncMock,
        side_effect=Exception("List error"),
    )
    async def test_migrate_from_objectstore_to_atlan_list_error(
        self,
        mock_list_keys: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test migration when listing files fails."""
        mock_create_store.return_value = MagicMock()

        with pytest.raises(Exception, match="Migration failed") as exc_info:
            await AtlanStorage.migrate_from_objectstore_to_atlan("test_prefix")
        assert "List error" in str(exc_info.value.__cause__)

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch(
        "application_sdk.services.atlan_storage.download_file",
        new_callable=AsyncMock,
        side_effect=Exception("Get data failed"),
    )
    @patch(
        "application_sdk.services.atlan_storage.list_keys",
        new_callable=AsyncMock,
        return_value=["file1.txt"],
    )
    async def test_migrate_from_objectstore_to_atlan_get_data_error(
        self,
        mock_list_keys: AsyncMock,
        mock_download: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test migration when downloading file fails."""
        mock_create_store.return_value = MagicMock()

        result = await AtlanStorage.migrate_from_objectstore_to_atlan("test_prefix")

        assert result.total_files == 1
        assert result.migrated_files == 0
        assert result.failed_migrations == 1
        assert len(result.failures) == 1
        assert result.failures[0]["file"] == "file1.txt"
        assert "Get data failed" in result.failures[0]["error"]

    @patch("application_sdk.services.atlan_storage.create_store_from_binding")
    @patch("application_sdk.services.atlan_storage.upload_file", new_callable=AsyncMock)
    @patch(
        "application_sdk.services.atlan_storage.download_file", new_callable=AsyncMock
    )
    @patch(
        "application_sdk.services.atlan_storage.list_keys",
        new_callable=AsyncMock,
        return_value=[f"file{i}.json" for i in range(10)],
    )
    async def test_parallel_file_migration(
        self,
        mock_list_keys: AsyncMock,
        mock_download: AsyncMock,
        mock_upload: AsyncMock,
        mock_create_store: MagicMock,
    ) -> None:
        """Test that files are migrated in parallel."""
        mock_create_store.return_value = MagicMock()

        result = await AtlanStorage.migrate_from_objectstore_to_atlan("test_prefix")

        assert result.total_files == 10
        assert result.migrated_files == 10
        assert mock_upload.call_count == 10
