"""Unit tests for ObjectStore services."""

from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from application_sdk.services.objectstore import ObjectStore


@pytest.mark.asyncio
class TestObjectStore:
    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_upload_file_success(self, mock_dapr_client: MagicMock) -> None:
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        test_file_content = b"test content"
        m = mock_open(read_data=test_file_content)

        with patch("builtins.open", m), patch(
            "os.path.exists", return_value=True
        ), patch("os.path.isfile", return_value=True), patch(
            "application_sdk.services.objectstore.ObjectStore._cleanup_local_path"
        ) as mock_cleanup:
            await ObjectStore.upload_file(
                source="/tmp/test.txt",
                destination="/prefix/test.txt",
            )

        mock_client.invoke_binding.assert_called_once()
        mock_cleanup.assert_called_once_with("/tmp/test.txt")

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_upload_directory_success(self, mock_dapr_client: MagicMock) -> None:
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        with patch("os.walk") as mock_walk, patch("os.path.isdir") as mock_isdir, patch(
            "os.path.exists", return_value=True
        ), patch("builtins.open", mock_open(read_data=b"x")), patch(
            "application_sdk.services.objectstore.ObjectStore._cleanup_local_path"
        ) as mock_cleanup:
            mock_isdir.return_value = True
            mock_walk.return_value = [("/input", [], ["file1.txt", "file2.txt"])]

            await ObjectStore.upload_prefix(
                source="/input",
                destination="/prefix",
            )

        assert mock_client.invoke_binding.call_count == 2
        assert mock_cleanup.call_count == 2

    @patch(
        "application_sdk.services.objectstore.ObjectStore.get_content",
        new_callable=AsyncMock,
    )
    async def test_download_file_success(self, mock_get_content: AsyncMock) -> None:
        mock_get_content.return_value = b"abc"
        with patch("builtins.open", mock_open()) as m, patch(
            "os.path.exists", return_value=True
        ), patch("os.path.dirname", return_value="/tmp"):
            await ObjectStore.download_file(
                source="/prefix/test.txt",
                destination="/tmp/test.txt",
            )
        m().write.assert_called_once_with(b"abc")

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_delete_file_success(self, mock_dapr_client: MagicMock) -> None:
        """Test successful deletion of a single file."""
        mock_client = MagicMock()
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        await ObjectStore.delete_file(key="test/file.txt")

        mock_client.invoke_binding.assert_called_once_with(
            binding_name="objectstore",
            operation="delete",
            data=b'{"key": "test/file.txt"}',
            binding_metadata={
                "key": "test/file.txt",
                "fileName": "test/file.txt",
                "blobName": "test/file.txt",
            },
        )

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_delete_file_failure(self, mock_dapr_client: MagicMock) -> None:
        """Test delete file failure handling."""
        mock_client = MagicMock()
        mock_client.invoke_binding.side_effect = Exception("Delete failed")
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        with pytest.raises(Exception, match="Delete failed"):
            await ObjectStore.delete_file(key="test/file.txt")

    @patch(
        "application_sdk.services.objectstore.ObjectStore.list_files",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.objectstore.ObjectStore.delete_file",
        new_callable=AsyncMock,
    )
    async def test_delete_prefix_success(
        self, mock_delete_file: AsyncMock, mock_list_files: AsyncMock
    ) -> None:
        """Test successful deletion of all files under a prefix."""
        mock_list_files.return_value = [
            "prefix/file1.txt",
            "prefix/file2.txt",
            "prefix/subdir/file3.txt",
        ]

        await ObjectStore.delete_prefix(prefix="prefix/")

        mock_list_files.assert_called_once_with(
            prefix="prefix/", store_name="objectstore"
        )
        assert mock_delete_file.call_count == 3
        mock_delete_file.assert_any_call(
            key="prefix/file1.txt", store_name="objectstore"
        )
        mock_delete_file.assert_any_call(
            key="prefix/file2.txt", store_name="objectstore"
        )
        mock_delete_file.assert_any_call(
            key="prefix/subdir/file3.txt", store_name="objectstore"
        )

    @patch(
        "application_sdk.services.objectstore.ObjectStore.list_files",
        new_callable=AsyncMock,
    )
    async def test_delete_prefix_empty(self, mock_list_files: AsyncMock) -> None:
        """Test delete prefix when no files exist under the prefix."""
        mock_list_files.return_value = []

        # Should not raise an exception
        await ObjectStore.delete_prefix(prefix="empty/prefix/")

        mock_list_files.assert_called_once_with(
            prefix="empty/prefix/", store_name="objectstore"
        )

    @patch(
        "application_sdk.services.objectstore.ObjectStore.list_files",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.objectstore.ObjectStore.delete_file",
        new_callable=AsyncMock,
    )
    async def test_delete_prefix_partial_failure(
        self, mock_delete_file: AsyncMock, mock_list_files: AsyncMock
    ) -> None:
        """Test delete prefix continues when individual file deletions fail."""
        mock_list_files.return_value = [
            "prefix/file1.txt",
            "prefix/file2.txt",
            "prefix/file3.txt",
        ]

        # Make the second file deletion fail
        def delete_side_effect(key, store_name):
            if "file2.txt" in key:
                raise Exception("Failed to delete file2.txt")

        mock_delete_file.side_effect = delete_side_effect

        # Should not raise an exception despite individual file failure
        await ObjectStore.delete_prefix(prefix="prefix/")

        mock_list_files.assert_called_once_with(
            prefix="prefix/", store_name="objectstore"
        )
        assert mock_delete_file.call_count == 3

    @patch(
        "application_sdk.services.objectstore.ObjectStore.list_files",
        new_callable=AsyncMock,
    )
    async def test_delete_prefix_list_failure(self, mock_list_files: AsyncMock) -> None:
        """Test delete prefix when listing files fails - should raise FileNotFoundError."""
        mock_list_files.side_effect = Exception("Failed to list files")

        # Should raise FileNotFoundError to give developers clear feedback
        with pytest.raises(
            FileNotFoundError, match="No files found under prefix: prefix/"
        ):
            await ObjectStore.delete_prefix(prefix="prefix/")

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_success(self, mock_dapr_client: MagicMock) -> None:
        """Test successful file content retrieval."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = b"test file content"
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        result = await ObjectStore.get_content(key="test/file.txt")

        assert result == b"test file content"
        mock_client.invoke_binding.assert_called_once_with(
            binding_name="objectstore",
            operation="get",
            data=b'{"key": "test/file.txt"}',
            binding_metadata={
                "key": "test/file.txt",
                "fileName": "test/file.txt",
                "blobName": "test/file.txt",
            },
        )

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_file_not_found_with_suppress_error_false(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test get_content raises exception when file not found and suppress_error=False."""
        mock_client = MagicMock()
        mock_client.invoke_binding.side_effect = Exception("File not found")
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        with pytest.raises(Exception, match="File not found"):
            await ObjectStore.get_content(
                key="nonexistent/file.txt", suppress_error=False
            )

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_file_not_found_with_suppress_error_true(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test get_content returns None when file not found and suppress_error=True."""
        mock_client = MagicMock()
        mock_client.invoke_binding.side_effect = Exception("File not found")
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        result = await ObjectStore.get_content(
            key="nonexistent/file.txt", suppress_error=True
        )

        assert result is None

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_no_data_with_suppress_error_false(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test get_content raises exception when no data returned and suppress_error=False."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = None
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        with pytest.raises(Exception, match="No data received for file: test/file.txt"):
            await ObjectStore.get_content(key="test/file.txt", suppress_error=False)

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_no_data_with_suppress_error_true(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test get_content returns None when no data returned and suppress_error=True."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = None
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        result = await ObjectStore.get_content(key="test/file.txt", suppress_error=True)

        assert result is None

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_empty_data_with_suppress_error_false(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test get_content raises exception when empty data returned and suppress_error=False."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = b""
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        with pytest.raises(Exception, match="No data received for file: test/file.txt"):
            await ObjectStore.get_content(key="test/file.txt", suppress_error=False)

    @patch("application_sdk.services.objectstore.DaprClient")
    async def test_get_content_empty_data_with_suppress_error_true(
        self, mock_dapr_client: MagicMock
    ) -> None:
        """Test get_content returns None when empty data returned and suppress_error=True."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = b""
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        result = await ObjectStore.get_content(key="test/file.txt", suppress_error=True)

        assert result is None

    # @patch("application_sdk.services.objectstore.ObjectStore.list_files", new_callable=AsyncMock)
    # @patch("application_sdk.services.objectstore.ObjectStore._download_file", new_callable=AsyncMock)
    # async def test_download_directory_success(
    #     self, mock_download_file: AsyncMock, mock_list_files: AsyncMock
    # ) -> None:
    #     mock_list_files.return_value = ["a.txt", "b.txt"]
    #     await ObjectStore.download(
    #         source="/prefix/",
    #         destination="/tmp",
    #     )
    #     assert mock_download_file.await_count == 2
