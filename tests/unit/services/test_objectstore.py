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
