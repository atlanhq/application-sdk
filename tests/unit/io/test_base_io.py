"""Unit tests for the base Reader and Writer classes."""

import os
from typing import Any, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.common.error_codes import IOError as SDKIOError
from application_sdk.common.types import DataframeType
from application_sdk.io import Reader, Writer
from application_sdk.io._utils import download_files, normalize_dict_input


class MockReader(Reader):
    """Mock implementation of Reader for testing."""

    def __init__(self, path: str, file_names: List[str] = None):
        self.path = path
        self.file_names = file_names
        self._EXTENSION = ".parquet"  # Default extension for testing

    async def read(self):
        """Mock implementation."""
        pass

    async def read_batches(self):
        """Mock implementation."""
        pass


class MockReaderNoPath(Reader):
    """Mock implementation without path attribute for testing."""

    def __init__(self):
        pass

    async def read(self):
        """Mock implementation."""
        pass

    async def read_batches(self):
        """Mock implementation."""
        pass


class TestReaderDownloadFiles:
    """Test cases for Reader.download_files method."""

    @pytest.mark.asyncio
    async def test_download_files_no_path_attribute(self):
        """Test that AttributeError is raised when input has no path attribute."""
        input_instance = MockReaderNoPath()

        with pytest.raises(
            AttributeError, match="'MockReaderNoPath' object has no attribute 'path'"
        ):
            await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

    @pytest.mark.asyncio
    async def test_download_files_empty_path(self):
        """Test behavior when path is empty."""
        input_instance = MockReader("")

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix",
            side_effect=Exception("Object store download failed"),
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

    @pytest.mark.asyncio
    async def test_download_files_local_single_file_exists(self):
        """Test successful local file discovery for single file."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with patch("os.path.isfile", return_value=True):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert result == [path]

    @pytest.mark.asyncio
    async def test_download_files_local_directory_exists(self):
        """Test successful local file discovery for directory."""
        path = "/data"
        input_instance = MockReader(path)
        expected_files = ["/data/file1.parquet", "/data/file2.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=expected_files):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_local_directory_with_file_names_filter(self):
        """Test local file discovery with file_names filtering."""
        path = "/data"
        file_names = ["file1.parquet", "file3.parquet"]
        input_instance = MockReader(path, file_names)
        all_files = [
            "/data/file1.parquet",
            "/data/file2.parquet",
            "/data/file3.parquet",
        ]
        expected_files = ["/data/file1.parquet", "/data/file3.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=all_files):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert set(result) == set(expected_files)

    @pytest.mark.asyncio
    async def test_download_files_single_file_with_file_names_match(self):
        """Test single file with file_names filter that matches."""
        path = "/data/test.parquet"
        file_names = ["test.parquet"]
        input_instance = MockReader(path, file_names)

        with patch("os.path.isfile", return_value=True):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

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
        input_instance = MockReader(path, file_names)

        # MockInput allows this configuration, and single file will be found locally
        with patch("os.path.isfile", return_value=True):
            # Local single file exists and will be returned (no filtering applied)
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )
            assert result == ["/data/test.parquet"]

    @pytest.mark.asyncio
    async def test_download_files_download_single_file_success(self):
        """Test successful download of single file from object store."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with patch("os.path.isfile", side_effect=[False, True]), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_download, patch(
            "application_sdk.activities.common.utils.get_object_store_prefix",
            return_value="data/test.parquet",
        ):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

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
        input_instance = MockReader(path)
        expected_files = ["/data/file1.parquet", "/data/file2.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix",
            new_callable=AsyncMock,
        ) as mock_download, patch(
            "application_sdk.activities.common.utils.get_object_store_prefix",
            return_value="data",
        ):
            # Mock the file finding function to return empty for local check, then files after download
            with patch(
                "application_sdk.io._utils.find_local_files_by_extension"
            ) as mock_find_files:
                # Use a function that returns different values based on the path
                def mock_find_files_func(path, extension, file_names=None):
                    if path == "/data":
                        return []  # Local check returns empty
                    else:
                        return expected_files  # After download returns files

                mock_find_files.side_effect = mock_find_files_func

                result = await download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

                mock_download.assert_called_once_with(
                    source="data", destination="./local/tmp/data"
                )
                assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_download_specific_files_success(self):
        """Test successful download of specific files from object store."""
        path = "/data"
        file_names = ["file1.parquet", "file2.parquet"]
        input_instance = MockReader(path, file_names)
        # Expected files will be in temporary directory after download
        # Normalize paths for cross-platform compatibility
        expected_files = [
            os.path.join("./local/tmp/data", "file1.parquet"),
            os.path.join("./local/tmp/data", "file2.parquet"),
        ]

        def mock_isfile(path):
            # Return False for initial local check, True for downloaded files
            # Normalize paths for cross-platform comparison
            expected_paths = [
                os.path.join("./local/tmp/data", "file1.parquet"),
                os.path.join("./local/tmp/data", "file2.parquet"),
            ]
            if path in expected_paths:
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
            "application_sdk.activities.common.utils.get_object_store_prefix",
            side_effect=lambda p: p.lstrip("/").replace("\\", "/"),
        ):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            # Should download each specific file
            # Normalize paths for cross-platform compatibility
            assert mock_download.call_count == 2
            mock_download.assert_any_call(
                source=os.path.join("data", "file1.parquet"),
                destination=os.path.join("./local/tmp/data", "file1.parquet"),
            )
            mock_download.assert_any_call(
                source=os.path.join("data", "file2.parquet"),
                destination=os.path.join("./local/tmp/data", "file2.parquet"),
            )
            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_download_failure(self):
        """Test download failure from object store."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
            side_effect=Exception("Download failed"),
        ), patch(
            "application_sdk.activities.common.utils.get_object_store_prefix",
            return_value="data/test.parquet",
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

    @pytest.mark.asyncio
    async def test_download_files_download_success_but_no_files_found(self):
        """Test download succeeds but no files found after download."""
        path = "/data"  # Use directory path
        input_instance = MockReader(path)

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix",
            new_callable=AsyncMock,
        ), patch(
            "application_sdk.activities.common.utils.get_object_store_prefix",
            return_value="data",
        ), patch(
            "application_sdk.io._utils.find_local_files_by_extension",
            side_effect=[
                [],
                [],
            ],  # Both calls (local check and after download) return []
        ):
            # Should raise error when no files found after download
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

    @pytest.mark.asyncio
    async def test_download_files_recursive_glob_pattern(self):
        """Test that recursive glob pattern is used for directory search."""
        path = "/data"
        input_instance = MockReader(path)
        expected_files = ["/data/subdir/file1.parquet", "/data/file2.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=expected_files) as mock_glob:
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            # Should use recursive glob pattern (OS-specific path separators)
            expected_pattern = os.path.join("/data", "**", "*.parquet")
            mock_glob.assert_called_once_with(expected_pattern, recursive=True)
            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_file_extension_filtering(self):
        """Test that only files with correct extension are returned."""
        path = "/data"
        input_instance = MockReader(path)
        expected_files = ["/data/file1.parquet", "/data/file3.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=expected_files):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_file_names_basename_matching(self):
        """Test file_names matching works with both full path and basename."""
        path = "/data"
        file_names = ["file1.parquet"]  # Just basename
        input_instance = MockReader(path, file_names)
        all_files = ["/data/subdir/file1.parquet", "/data/file2.parquet"]
        expected_files = ["/data/subdir/file1.parquet"]

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=True
        ), patch("glob.glob", return_value=all_files):
            result = await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            assert result == expected_files

    @pytest.mark.asyncio
    async def test_download_files_logging_messages(self):
        """Test that appropriate logging messages are generated."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with patch("os.path.isfile", return_value=True), patch(
            "application_sdk.io._utils.logger"
        ) as mock_logger:
            await download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            mock_logger.info.assert_called_with(
                "Found 1 .parquet files locally at: /data/test.parquet"
            )

    @pytest.mark.asyncio
    async def test_download_files_logging_download_attempt(self):
        """Test logging when attempting download from object store."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with patch("os.path.isfile", return_value=False), patch(
            "os.path.isdir", return_value=False
        ), patch("glob.glob", return_value=[]), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
            side_effect=Exception("Download failed"),
        ), patch(
            "application_sdk.activities.common.utils.get_object_store_prefix",
            return_value="data/test.parquet",
        ), patch("application_sdk.io._utils.logger") as mock_logger:
            with pytest.raises(SDKIOError):
                await download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

            mock_logger.info.assert_any_call(
                "No local .parquet files found at /data/test.parquet, checking object store..."
            )
            mock_logger.error.assert_called_with(
                "Failed to download from object store: Download failed"
            )


class TestNormalizeDictInput:
    """Test cases for normalize_dict_input utility function."""

    def test_normalize_single_dict(self) -> None:
        """Test normalizing a single dictionary."""
        data = {"id": 1, "name": "test"}
        result = normalize_dict_input(data)
        assert result == [{"id": 1, "name": "test"}]

    def test_normalize_list_of_dicts(self) -> None:
        """Test normalizing a list of dictionaries."""
        data = [{"id": 1}, {"id": 2}]
        result = normalize_dict_input(data)
        assert result == [{"id": 1}, {"id": 2}]

    def test_normalize_empty_list(self) -> None:
        """Test normalizing an empty list."""
        data: list = []
        result = normalize_dict_input(data)
        assert result == []

    def test_normalize_invalid_input_string(self) -> None:
        """Test normalizing invalid input (string)."""
        data = "not_a_dict"
        result = normalize_dict_input(data)
        assert result is None

    def test_normalize_invalid_input_list_with_non_dict(self) -> None:
        """Test normalizing invalid input (list with non-dict items)."""
        data = [{"id": 1}, "not_a_dict", {"id": 2}]
        result = normalize_dict_input(data)
        assert result is None

    def test_normalize_invalid_input_number(self) -> None:
        """Test normalizing invalid input (number)."""
        data = 123
        result = normalize_dict_input(data)
        assert result is None

    def test_normalize_invalid_input_none(self) -> None:
        """Test normalizing invalid input (None)."""
        data = None
        result = normalize_dict_input(data)
        assert result is None


class MockWriter(Writer):
    """Mock implementation of Writer for testing."""

    def __init__(self, dataframe_type: DataframeType = DataframeType.pandas):
        self.output_path = "/tmp/test"
        self.output_prefix = ""
        self.total_record_count = 0
        self.chunk_count = 0
        self.chunk_part = 0
        self.buffer_size = 1000
        self.max_file_size_bytes = 1000000
        self.current_buffer_size = 0
        self.current_buffer_size_bytes = 0
        self.partitions: List[int] = []
        self.extension = ".json"
        self.dataframe_type = dataframe_type
        self.metrics = Mock()

    async def _write_dataframe(self, dataframe: Any, **kwargs: Any) -> None:
        """Mock implementation."""
        self.total_record_count += len(dataframe)

    async def _write_daft_dataframe(self, dataframe: Any, **kwargs: Any) -> None:
        """Mock implementation."""
        pass


class TestWriterDictHandling:
    """Test cases for base Writer dictionary handling."""

    @pytest.mark.asyncio
    async def test_write_single_dict(self) -> None:
        """Test writing a single dictionary through base Writer."""
        writer = MockWriter(dataframe_type=DataframeType.dict)
        data = {"id": 1, "name": "test"}

        await writer.write(data)

        assert writer.total_record_count == 1

    @pytest.mark.asyncio
    async def test_write_list_of_dicts(self) -> None:
        """Test writing a list of dictionaries through base Writer."""
        writer = MockWriter(dataframe_type=DataframeType.dict)
        data = [{"id": 1}, {"id": 2}]

        await writer.write(data)

        assert writer.total_record_count == 2

    @pytest.mark.asyncio
    async def test_write_empty_dict(self) -> None:
        """Test writing an empty dictionary list through base Writer."""
        writer = MockWriter(dataframe_type=DataframeType.dict)
        data: list = []

        await writer.write(data)

        # Should return without doing anything for empty list
        assert writer.total_record_count == 0

    @pytest.mark.asyncio
    async def test_write_invalid_dict_input(self) -> None:
        """Test writing invalid dictionary input raises error."""
        writer = MockWriter(dataframe_type=DataframeType.dict)
        invalid_data = "not_a_dict"

        with pytest.raises(ValueError, match="Invalid dictionary input"):
            await writer.write(invalid_data)  # type: ignore

    @pytest.mark.asyncio
    async def test_write_batches_dicts(self) -> None:
        """Test writing batches of dictionaries through base Writer."""
        writer = MockWriter(dataframe_type=DataframeType.dict)

        def dict_generator():
            yield {"id": 1}
            yield [{"id": 2}, {"id": 3}]

        await writer.write_batches(dict_generator())

        # Should have written 3 records total (1 + 2)
        assert writer.total_record_count == 3

    @pytest.mark.asyncio
    async def test_write_batches_empty_dicts(self) -> None:
        """Test writing batches with empty dictionary lists."""
        writer = MockWriter(dataframe_type=DataframeType.dict)

        def dict_generator():
            yield {"id": 1}
            yield []  # Empty list should be skipped
            yield [{"id": 2}]

        await writer.write_batches(dict_generator())

        # Should have written 2 records (empty list skipped)
        assert writer.total_record_count == 2

    @pytest.mark.asyncio
    async def test_write_batches_invalid_dict_input(self) -> None:
        """Test writing batches with invalid dictionary input raises error."""
        writer = MockWriter(dataframe_type=DataframeType.dict)

        def dict_generator():
            yield {"id": 1}
            yield "not_a_dict"  # Invalid input

        with pytest.raises(ValueError, match="Invalid dictionary input"):
            await writer.write_batches(dict_generator())

    @pytest.mark.asyncio
    async def test_write_batches_async_generator(self) -> None:
        """Test writing batches with async generator of dictionaries."""
        writer = MockWriter(dataframe_type=DataframeType.dict)

        async def dict_generator():
            yield {"id": 1}
            yield [{"id": 2}, {"id": 3}]

        await writer.write_batches(dict_generator())

        # Should have written 3 records total (1 + 2)
        assert writer.total_record_count == 3
