"""Unit tests for ObjectStore services."""

import os
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH
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

    @patch("application_sdk.services.objectstore.DaprClient")
    @patch("application_sdk.services.objectstore.logger")
    async def test_invoke_dapr_binding_with_large_data_warning(
        self, mock_logger: MagicMock, mock_dapr_client: MagicMock
    ) -> None:
        """Test that a warning is logged when data size exceeds DAPR_MAX_GRPC_MESSAGE_LENGTH."""

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = b"response data"
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        # Create data that exceeds the DAPR limit
        large_data = b"x" * (DAPR_MAX_GRPC_MESSAGE_LENGTH + 1)

        result = await ObjectStore._invoke_dapr_binding(
            operation="create",
            metadata={"key": "test"},
            data=large_data,
        )

        # Verify warning was logged
        mock_logger.warning.assert_called_once()
        warning_call = mock_logger.warning.call_args[0][0]
        assert "exceeds DAPR_MAX_GRPC_MESSAGE_LENGTH" in warning_call
        assert f"{DAPR_MAX_GRPC_MESSAGE_LENGTH + 1:,}" in warning_call

        # Verify DaprClient was called with increased max_grpc_message_length
        # With 5% buffer: (DAPR_MAX_GRPC_MESSAGE_LENGTH + 1) + 5% of (DAPR_MAX_GRPC_MESSAGE_LENGTH + 1)
        mock_dapr_client.assert_called_once()
        call_kwargs = mock_dapr_client.call_args[1]
        expected_data_size = DAPR_MAX_GRPC_MESSAGE_LENGTH + 1
        expected_buffer = max(
            int(expected_data_size * 0.05), 1024
        )  # 5% buffer, min 1KB
        expected_max_length = expected_data_size + expected_buffer
        assert call_kwargs["max_grpc_message_length"] == expected_max_length

        # Verify the binding was invoked
        mock_client.invoke_binding.assert_called_once()
        assert result == b"response data"

    @patch("application_sdk.services.objectstore.DaprClient")
    @patch("application_sdk.services.objectstore.logger")
    async def test_invoke_dapr_binding_with_small_data_no_warning(
        self, mock_logger: MagicMock, mock_dapr_client: MagicMock
    ) -> None:
        """Test that no warning is logged when data size is within DAPR_MAX_GRPC_MESSAGE_LENGTH."""
        from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = b"response data"
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        # Create data that is within the DAPR limit
        small_data = b"x" * (DAPR_MAX_GRPC_MESSAGE_LENGTH - 1000)

        result = await ObjectStore._invoke_dapr_binding(
            operation="create",
            metadata={"key": "test"},
            data=small_data,
        )

        # Verify no warning was logged
        mock_logger.warning.assert_not_called()

        # Verify DaprClient was called with default max_grpc_message_length
        mock_dapr_client.assert_called_once()
        call_kwargs = mock_dapr_client.call_args[1]
        assert call_kwargs["max_grpc_message_length"] == DAPR_MAX_GRPC_MESSAGE_LENGTH

        # Verify the binding was invoked
        mock_client.invoke_binding.assert_called_once()
        assert result == b"response data"

    @patch("application_sdk.services.objectstore.DaprClient")
    @patch("application_sdk.services.objectstore.logger")
    async def test_invoke_dapr_binding_with_string_data(
        self, mock_logger: MagicMock, mock_dapr_client: MagicMock
    ) -> None:
        """Test that string data is properly handled and size is calculated correctly."""
        from application_sdk.constants import DAPR_MAX_GRPC_MESSAGE_LENGTH

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = b"response data"
        mock_client.invoke_binding.return_value = mock_response
        mock_dapr_client.return_value.__enter__.return_value = mock_client

        # Create a string that when encoded exceeds the DAPR limit
        # Each character in UTF-8 is typically 1 byte, but we'll use a large string
        large_string = "x" * (DAPR_MAX_GRPC_MESSAGE_LENGTH + 1)

        result = await ObjectStore._invoke_dapr_binding(
            operation="create",
            metadata={"key": "test"},
            data=large_string,
        )

        # Verify warning was logged
        mock_logger.warning.assert_called_once()

        # Verify DaprClient was called with increased max_grpc_message_length
        mock_dapr_client.assert_called_once()
        call_kwargs = mock_dapr_client.call_args[1]
        # The encoded string size should be accounted for
        assert (
            call_kwargs["max_grpc_message_length"] >= DAPR_MAX_GRPC_MESSAGE_LENGTH + 1
        )

        # Verify the binding was invoked
        mock_client.invoke_binding.assert_called_once()
        assert result == b"response data"

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

    @pytest.mark.parametrize(
        "response_json, prefix, expected_paths",
        [
            # 1: Standard List of Strings
            (["file1.txt", "sub/file2.txt"], "", ["file1.txt", "sub/file2.txt"]),
            # 2: AWS S3 Format (Dict with Contents)
            (
                {"Contents": [{"Key": "s3-file.txt"}, {"Key": "folder/s3-file2.txt"}]},
                "",
                ["s3-file.txt", "folder/s3-file2.txt"],
            ),
            # 3: Azure/GCP Format (List of Dicts with "Name")
            (
                [
                    {"Name": "az-blob.txt", "etag": "123"},
                    {"Name": "folder/az-blob2.txt"},
                    {"Name": "folder/gcs.parquet"},
                ],
                "",
                ["az-blob.txt", "folder/az-blob2.txt", "folder/gcs.parquet"],
            ),
            # 4: Generic Wrapper "files"
            ({"files": ["generic1.txt"]}, "", ["generic1.txt"]),
            # 5: Generic Wrapper "keys"
            ({"keys": ["key1.txt"]}, "", ["key1.txt"]),
            # 6: Mixed Invalid Types (Integers, None, Boolean)
            (
                ["valid.txt", 123, None, True, "valid2.txt"],
                "",
                ["valid.txt", "valid2.txt"],
            ),
            # 7: Malformed Dictionaries (Missing "Name")
            (
                [{"Name": "valid.txt"}, {"etag": "no-name"}, {"size": 500}],
                "",
                ["valid.txt"],
            ),
            # 8: Dict with non-string "Name" value (None, int, nested dict)
            (
                [
                    {"Name": "valid.txt"},
                    {"Name": None},
                    {"Name": 123},
                    {"Name": {"nested": "dict"}},
                    {},
                ],
                "",
                ["valid.txt"],
            ),
            # 9: S3 Items Missing "Key" (Filtered by list comprehension)
            (
                {"Contents": [{"Key": "good.txt"}, {"Size": 500}]},
                "",
                ["good.txt"],
            ),
            # 10: Unknown Structure (Empty Fallback)
            ({"random": "data"}, "", []),
            # 11: Prefix Slicing (Prefix Exists in Path)
            # Logic: Returns substring starting from prefix
            (
                ["root/my-folder/image.png"],
                "my-folder",
                ["my-folder/image.png"],
            ),
            # 12: Prefix Fallback (Prefix NOT in Path)
            # Logic: Returns os.path.basename if prefix is provided but not found
            (
                ["other-location/image.png"],
                "my-folder/",
                ["image.png"],
            ),
            # 13: No Prefix Provided
            # Logic: Returns full original path
            (
                ["/full/path/to/file.txt"],
                "",
                ["/full/path/to/file.txt"],
            ),
        ],
    )
    @patch("application_sdk.services.objectstore.ObjectStore._invoke_dapr_binding")
    async def test_list_files_parsing_and_logic(
        self,
        mock_invoke: AsyncMock,
        response_json,
        prefix,
        expected_paths,
    ) -> None:
        """
        1. JSON Structure parsing (List vs Dict vs S3 vs Azure)
        2. Item validation (Skipping invalid types)
        3. Prefix path transformation logic
        """
        # Arrange
        import orjson

        mock_invoke.return_value = orjson.dumps(response_json)

        # Act
        result = await ObjectStore.list_files(prefix=prefix)

        # Assert
        assert result == expected_paths
        mock_invoke.assert_called_once()

    @patch("application_sdk.services.objectstore.ObjectStore._invoke_dapr_binding")
    async def test_list_files_empty_response(self, mock_invoke: AsyncMock) -> None:
        """14: Test handling of empty/None response from Dapr."""
        # Arrange: simulate empty byte response
        mock_invoke.return_value = b""

        # Act
        result = await ObjectStore.list_files(prefix="test")

        # Assert
        assert result == []

    @patch("application_sdk.services.objectstore.ObjectStore._invoke_dapr_binding")
    async def test_list_files_malformed_json(self, mock_invoke: AsyncMock) -> None:
        """15: Test handling of corrupt JSON response."""
        # Arrange: simulate invalid JSON
        mock_invoke.return_value = b"{invalid-json..."

        # Act & Assert
        with pytest.raises(Exception):  # Expecting JSONDecodeError wrapped or raised
            await ObjectStore.list_files(prefix="test")

    @patch("application_sdk.services.objectstore.ObjectStore._invoke_dapr_binding")
    async def test_list_files_binding_failure(self, mock_invoke: AsyncMock) -> None:
        """16: Test handling of Dapr binding exception."""
        # Arrange
        mock_invoke.side_effect = Exception("Network Error")

        # Act & Assert
        with pytest.raises(Exception, match="Network Error"):
            await ObjectStore.list_files(prefix="test")

    @patch("application_sdk.services.objectstore.os.sep", "\\")
    def test_normalize_object_store_key_windows_path(self) -> None:
        """Test _normalize_object_store_key converts Windows backslashes to forward slashes."""
        # Arrange
        windows_path = "artifacts\\apps\\default\\workflows\\wf123\\file.json"

        # Act
        result = ObjectStore._normalize_object_store_key(windows_path)

        # Assert
        assert result == "artifacts/apps/default/workflows/wf123/file.json"
        assert "\\" not in result

    def test_normalize_object_store_key_unix_path(self) -> None:
        """Test _normalize_object_store_key leaves Unix paths unchanged."""
        # Arrange
        unix_path = "artifacts/apps/default/workflows/wf123/file.json"

        # Act
        result = ObjectStore._normalize_object_store_key(unix_path)

        # Assert
        assert result == unix_path

    @patch("application_sdk.services.objectstore.os.sep", "\\")
    def test_normalize_object_store_key_mixed_path(self) -> None:
        """Test _normalize_object_store_key handles mixed separators on Windows."""
        # Arrange - on Windows, os.sep is backslash, so only backslashes get replaced
        mixed_path = "artifacts/apps\\default/workflows\\wf123\\file.json"

        # Act
        result = ObjectStore._normalize_object_store_key(mixed_path)

        # Assert - backslashes replaced, forward slashes unchanged
        assert result == "artifacts/apps/default/workflows/wf123/file.json"
        assert "\\" not in result

    @patch(
        "application_sdk.services.objectstore.ObjectStore.list_files",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.objectstore.ObjectStore.download_file",
        new_callable=AsyncMock,
    )
    async def test_download_prefix_without_destination(
        self, mock_download_file: AsyncMock, mock_list_files: AsyncMock
    ) -> None:
        """Test download_prefix uses default TEMPORARY_PATH when destination not provided."""
        from application_sdk.constants import TEMPORARY_PATH

        # Arrange
        source_prefix = "artifacts/wf/wf123/raw/models"
        mock_list_files.return_value = [f"{source_prefix}/file.json"]

        # Act - call without destination parameter
        await ObjectStore.download_prefix(
            source=source_prefix,
            store_name="objectstore",
        )

        # Assert: download_file should be called with TEMPORARY_PATH as base destination
        # download_file is called with positional args: (source, destination, store_name)
        mock_download_file.assert_called_once()
        call_args = mock_download_file.call_args[0]  # positional args
        destination_arg = call_args[1]  # second positional arg is destination
        assert TEMPORARY_PATH in destination_arg

    @patch("application_sdk.services.objectstore.os.sep", "\\")
    @patch("application_sdk.services.objectstore.ObjectStore._invoke_dapr_binding")
    async def test_list_files_normalizes_windows_prefix(
        self, mock_invoke: AsyncMock
    ) -> None:
        """Test list_files normalizes Windows-style prefix before path comparison.

        When running on Windows, a prefix with backslashes should still match
        paths returned from object store (which always use forward slashes).
        """
        import orjson

        # Arrange: prefix with Windows backslashes, paths with forward slashes
        windows_prefix = "artifacts\\apps\\default"
        object_store_paths = [
            "artifacts/apps/default/file1.json",
            "artifacts/apps/default/subdir/file2.json",
        ]
        mock_invoke.return_value = orjson.dumps(object_store_paths)

        # Act
        result = await ObjectStore.list_files(prefix=windows_prefix)

        # Assert: paths should be correctly extracted starting from the normalized prefix
        # Without the fix, this would return ["file1.json", "file2.json"] (basename fallback)
        assert result == [
            "artifacts/apps/default/file1.json",
            "artifacts/apps/default/subdir/file2.json",
        ]

    @patch("application_sdk.services.objectstore.os.sep", "\\")
    @patch(
        "application_sdk.services.objectstore.ObjectStore.list_files",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.objectstore.ObjectStore.download_file",
        new_callable=AsyncMock,
    )
    async def test_download_prefix_normalizes_windows_path(
        self, mock_download_file: AsyncMock, mock_list_files: AsyncMock
    ) -> None:
        """Test download_prefix normalizes Windows-style prefix before path comparison.

        When running on Windows, a prefix with backslashes should still match
        paths returned from object store (which always use forward slashes).
        The relative path extraction should work correctly after normalization.
        """
        from application_sdk.constants import TEMPORARY_PATH

        # Arrange: prefix with Windows backslashes, paths with forward slashes
        windows_prefix = "artifacts\\apps\\default\\workflows\\wf123"
        object_store_paths = [
            "artifacts/apps/default/workflows/wf123/file1.json",
            "artifacts/apps/default/workflows/wf123/subdir/file2.json",
            "artifacts/apps/default/workflows/wf123/models/model.parquet",
        ]
        mock_list_files.return_value = object_store_paths

        # Act
        await ObjectStore.download_prefix(
            source=windows_prefix,
            destination=TEMPORARY_PATH,
            store_name="objectstore",
        )

        # Assert: list_files should be called with the Windows prefix (not normalized)
        mock_list_files.assert_called_once_with(windows_prefix, "objectstore")

        # Assert: download_file should be called 3 times with correct relative paths
        assert mock_download_file.call_count == 3

        # Verify first file: file1.json -> relative path should be "file1.json"
        call1_args = mock_download_file.call_args_list[0][0]
        assert call1_args[0] == "artifacts/apps/default/workflows/wf123/file1.json"
        # Construct expected path the same way the implementation does: os.path.join(destination, relative_path)
        # where relative_path has forward slashes from normalized object store path
        expected_path1 = os.path.join(TEMPORARY_PATH, "file1.json")
        assert call1_args[1] == expected_path1
        assert call1_args[2] == "objectstore"

        # Verify second file: subdir/file2.json -> relative path should be "subdir/file2.json"
        call2_args = mock_download_file.call_args_list[1][0]
        assert (
            call2_args[0] == "artifacts/apps/default/workflows/wf123/subdir/file2.json"
        )
        # relative_path is "subdir/file2.json" (with forward slashes), os.path.join handles it
        expected_path2 = os.path.join(TEMPORARY_PATH, "subdir/file2.json")
        assert call2_args[1] == expected_path2
        assert call2_args[2] == "objectstore"

        # Verify third file: models/model.parquet -> relative path should be "models/model.parquet"
        call3_args = mock_download_file.call_args_list[2][0]
        assert (
            call3_args[0]
            == "artifacts/apps/default/workflows/wf123/models/model.parquet"
        )
        # relative_path is "models/model.parquet" (with forward slashes), os.path.join handles it
        expected_path3 = os.path.join(TEMPORARY_PATH, "models/model.parquet")
        assert call3_args[1] == expected_path3
        assert call3_args[2] == "objectstore"
