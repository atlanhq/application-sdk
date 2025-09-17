"""Unit tests for the base Input class download_files method."""

import os
from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.error_codes import IOError as SDKIOError
from application_sdk.inputs import Input


class MockInput(Input):
    """Mock implementation of Input for testing."""

    def __init__(self, path: str, file_names: List[str] = None):
        self.path = path
        self.file_names = file_names
        self._EXTENSION = ".parquet"  # Default extension for testing

    async def get_batched_dataframe(self):
        """Mock implementation."""
        pass

    async def get_dataframe(self):
        """Mock implementation."""
        pass

    async def get_batched_daft_dataframe(self):
        """Mock implementation."""
        pass

    async def get_daft_dataframe(self):
        """Mock implementation."""
        pass


class MockInputNoPath(Input):
    """Mock implementation without path attribute for testing."""

    def __init__(self):
        pass

    async def get_batched_dataframe(self):
        """Mock implementation."""
        pass

    async def get_dataframe(self):
        """Mock implementation."""
        pass

    async def get_batched_daft_dataframe(self):
        """Mock implementation."""
        pass

    async def get_daft_dataframe(self):
        """Mock implementation."""
        pass


class TestInputDownloadFiles:
    """Test cases for Input.download_files method."""

    @pytest.mark.asyncio
    async def test_download_files_no_path_attribute(self):
        """Test that AttributeError is raised when input has no path attribute."""
        input_instance = MockInputNoPath()

        with pytest.raises(
            AttributeError, match="'MockInputNoPath' object has no attribute 'path'"
        ):
            await input_instance.download_files()

    @pytest.mark.asyncio
    async def test_download_files_empty_path(self):
        """Test behavior when path is empty."""
        input_instance = MockInput("")

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix",
            side_effect=Exception("Object store download failed"),
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await input_instance.download_files()

    @pytest.mark.asyncio
    async def test_download_files_local_single_file_exists(self):
        """Test successful local file discovery for single file."""
        path = "/data/test.parquet"
        input_instance = MockInput(path)

        with patch("os.path.isfile", return_value=True):
            result = await input_instance.download_files()

        assert result == [path]

    @pytest.mark.asyncio
    async def test_download_files_local_directory_exists(self):
        """Test successful local file discovery for directory."""
        path = "/data"
        input_instance = MockInput(path)
        expected_files = ["/data/file1.parquet", "/data/file2.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=expected_files):
            result = await input_instance.download_files()

        assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_local_directory_with_file_names_filter(self):
        """Test local file discovery with file_names filtering."""
        path = "/data"
        file_names = ["file1.parquet", "file3.parquet"]
        input_instance = MockInput(path, file_names)
        all_files = [
            "/data/file1.parquet",
            "/data/file2.parquet",
            "/data/file3.parquet",
        ]
        expected_files = ["/data/file1.parquet", "/data/file3.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=all_files):
            result = await input_instance.download_files()

        assert set(result) == set(expected_files)

    @pytest.mark.asyncio
    async def test_download_files_single_file_with_file_names_match(self):
        """Test single file with file_names filter that matches."""
        path = "/data/test.parquet"
        file_names = ["test.parquet"]
        input_instance = MockInput(path, file_names)

        with patch("os.path.isfile", return_value=True):
            result = await input_instance.download_files()

        assert result == [path]

    @pytest.mark.asyncio
    async def test_download_files_single_file_with_file_names_no_filtering(self):
        """Test single file with file_names - MockInput allows this but single files are not filtered."""
        # This test documents that MockInput allows single file + file_names configuration
        # Real inputs (JsonInput, ParquetInput) prevent this at construction level
        # But for single files, file_names filtering is not applied (validation prevents this scenario)

        path = "/data/test.parquet"
        file_names = [
            "other.parquet"
        ]  # This doesn't match the file, but won't be used for filtering
        input_instance = MockInput(path, file_names)

        # MockInput allows this configuration, and single file will be found locally
        with patch("os.path.isfile", return_value=True):
            # Local single file exists and will be returned (no filtering applied)
            result = await input_instance.download_files()
            assert result == ["/data/test.parquet"]

    @pytest.mark.asyncio
    async def test_download_files_download_single_file_success(self):
        """Test successful download of single file from object store."""
        path = "/data/test.parquet"
        input_instance = MockInput(path)

        with patch("os.path.isfile", side_effect=[False, True]), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_download, patch(
            "application_sdk.inputs.get_object_store_prefix",
            return_value="data/test.parquet",
        ):
            result = await input_instance.download_files()

            mock_download.assert_called_once_with(
                source="data/test.parquet", destination="./local/tmp/data/test.parquet"
            )
            # Result should be the actual downloaded file path in temporary directory
            expected_path = "./local/tmp/data/test.parquet"
            assert result == [expected_path]

    @pytest.mark.asyncio
    async def test_download_files_download_directory_success(self):
        """Test successful download of directory from object store."""
        path = "/data"
        input_instance = MockInput(path)
        expected_files = ["/data/file1.parquet", "/data/file2.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix",
            new_callable=AsyncMock,
        ) as mock_download, patch(
            "application_sdk.inputs.get_object_store_prefix",
            return_value="data",
        ):
            # Mock the file finding function to return empty for local check, then files after download
            with patch(
                "application_sdk.inputs.find_local_files_by_extension"
            ) as mock_find_files:
                # Use a function that returns different values based on the path
                def mock_find_files_func(path, extension, file_names=None):
                    if path == "/data":
                        return []  # Local check returns empty
                    else:
                        return expected_files  # After download returns files

                mock_find_files.side_effect = mock_find_files_func

                result = await input_instance.download_files()

                mock_download.assert_called_once_with(
                    source="data", destination="./local/tmp/data"
                )
                assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_download_specific_files_success(self):
        """Test successful download of specific files from object store."""
        path = "/data"
        file_names = ["file1.parquet", "file2.parquet"]
        input_instance = MockInput(path, file_names)
        # Expected files will be in temporary directory after download
        expected_files = [
            "./local/tmp/data/file1.parquet",
            "./local/tmp/data/file2.parquet",
        ]

        def mock_isfile(path):
            # Return False for initial local check, True for downloaded files
            if path in [
                "./local/tmp/data/file1.parquet",
                "./local/tmp/data/file2.parquet",
            ]:
                return True
            return False

        with patch("os.path.isfile", side_effect=mock_isfile), patch(
            "os.path.isdir", return_value=True
        ), patch(
            "glob.glob",
            side_effect=[[]],  # Only for initial local check
        ), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_download, patch(
            "application_sdk.inputs.get_object_store_prefix",
            side_effect=lambda p: p.lstrip("/").replace("\\", "/"),
        ):
            result = await input_instance.download_files()

            # Should download each specific file
            assert mock_download.call_count == 2
            mock_download.assert_any_call(
                source="data/file1.parquet",
                destination="./local/tmp/data/file1.parquet",
            )
            mock_download.assert_any_call(
                source="data/file2.parquet",
                destination="./local/tmp/data/file2.parquet",
            )
            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_download_failure(self):
        """Test download failure from object store."""
        path = "/data/test.parquet"
        input_instance = MockInput(path)

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
            side_effect=Exception("Download failed"),
        ), patch(
            "application_sdk.inputs.get_object_store_prefix",
            return_value="data/test.parquet",
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await input_instance.download_files()

    @pytest.mark.asyncio
    async def test_download_files_download_success_but_no_files_found(self):
        """Test download succeeds but no files found after download."""
        path = "/data"  # Use directory path
        input_instance = MockInput(path)

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix",
            new_callable=AsyncMock,
        ), patch(
            "application_sdk.inputs.get_object_store_prefix",
            return_value="data",
        ), patch(
            "application_sdk.inputs.find_local_files_by_extension",
            side_effect=[
                [],
                [],
            ],  # Both calls (local check and after download) return []
        ):
            # Should raise error when no files found after download
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await input_instance.download_files()

    @pytest.mark.asyncio
    async def test_download_files_recursive_glob_pattern(self):
        """Test that recursive glob pattern is used for directory search."""
        path = "/data"
        input_instance = MockInput(path)
        expected_files = ["/data/subdir/file1.parquet", "/data/file2.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=expected_files) as mock_glob:
            result = await input_instance.download_files()

            # Should use recursive glob pattern (OS-specific path separators)
            expected_pattern = os.path.join("/data", "**", "*.parquet")
            mock_glob.assert_called_once_with(expected_pattern, recursive=True)
            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_file_extension_filtering(self):
        """Test that only files with correct extension are returned."""
        path = "/data"
        input_instance = MockInput(path)
        expected_files = ["/data/file1.parquet", "/data/file3.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=expected_files):
            result = await input_instance.download_files()

            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_file_names_basename_matching(self):
        """Test file_names matching works with both full path and basename."""
        path = "/data"
        file_names = ["file1.parquet"]  # Just basename
        input_instance = MockInput(path, file_names)
        all_files = ["/data/subdir/file1.parquet", "/data/file2.parquet"]
        expected_files = ["/data/subdir/file1.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=all_files):
            result = await input_instance.download_files()

            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_logging_messages(self):
        """Test that appropriate logging messages are generated."""
        path = "/data/test.parquet"
        input_instance = MockInput(path)

        with patch("os.path.isfile", return_value=True), patch(
            "application_sdk.inputs.logger"
        ) as mock_logger:
            await input_instance.download_files()

            mock_logger.info.assert_called_with(
                "Found 1 .parquet files locally at: /data/test.parquet"
            )

    @pytest.mark.asyncio
    async def test_download_files_logging_download_attempt(self):
        """Test logging when attempting download from object store."""
        path = "/data/test.parquet"
        input_instance = MockInput(path)

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
            side_effect=Exception("Download failed"),
        ), patch(
            "application_sdk.inputs.get_object_store_prefix",
            return_value="data/test.parquet",
        ), patch("application_sdk.inputs.logger") as mock_logger:
            with pytest.raises(SDKIOError):
                await input_instance.download_files()

            mock_logger.info.assert_any_call(
                "No local .parquet files found at /data/test.parquet, checking object store..."
            )
            mock_logger.error.assert_called_with(
                "Failed to download from object store: Download failed"
            )
