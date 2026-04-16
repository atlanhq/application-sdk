"""Unit tests for the base Reader and Writer classes."""

import os
import tempfile
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.common.error_codes import IOError as SDKIOError
from application_sdk.storage.formats import Reader
from application_sdk.storage.formats.utils import (
    _download_files,
    find_local_files_by_extension,
)

# Fixed UUID used in tests so download paths are deterministic
_FIXED_UUID_HEX = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
_FIXED_DOWNLOAD_ID = _FIXED_UUID_HEX[:12]  # "a1b2c3d4e5f6"

# Path normalises "./foo" → "foo", so expected destinations drop the leading "./"
# _MOCK_STORE is referenced in _resolve_store patches throughout this module.
_MOCK_STORE = MagicMock()
_EXPECTED_TMP = str(Path("./local/tmp/") / _FIXED_DOWNLOAD_ID)


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
    """Test cases for Reader._download_files method."""

    @pytest.mark.asyncio
    async def test__download_files_no_path_attribute(self):
        """Test that AttributeError is raised when input has no path attribute."""
        input_instance = MockReaderNoPath()

        with pytest.raises(
            AttributeError, match="'MockReaderNoPath' object has no attribute 'path'"
        ):
            await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

    @pytest.mark.asyncio
    async def test__download_files_empty_path(self):
        """Test behavior when path is empty."""
        input_instance = MockReader("")

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=False),
            patch("glob.glob", return_value=[]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                side_effect=Exception("Object store download failed"),
            ),
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await _download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

    @pytest.mark.asyncio
    async def test__download_files_local_single_file_exists(self):
        """Test successful local file discovery for single file."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with patch("os.path.isfile", return_value=True):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert result == [path]

    @pytest.mark.asyncio
    async def test__download_files_local_directory_exists(self):
        """Test successful local file discovery for directory."""
        path = "/data"
        input_instance = MockReader(path)
        expected_files = ["/data/file1.parquet", "/data/file2.parquet"]

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=expected_files),
        ):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert result == expected_files

    @pytest.mark.asyncio
    async def test__download_files_local_directory_with_file_names_filter(self):
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

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=all_files),
        ):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert set(result) == set(expected_files)

    @pytest.mark.asyncio
    async def test__download_files_single_file_with_file_names_match(self):
        """Test single file with file_names filter that matches."""
        path = "/data/test.parquet"
        file_names = ["test.parquet"]
        input_instance = MockReader(path, file_names)

        with patch("os.path.isfile", return_value=True):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

        assert result == [path]

    @pytest.mark.asyncio
    async def test__download_files_single_file_with_file_names_no_filtering(self):
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
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )
            assert result == ["/data/test.parquet"]

    @pytest.mark.asyncio
    async def test__download_files_download_single_file_success(self):
        """Test successful download of single file from object store."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with (
            patch("os.path.isfile", side_effect=[False, True]),
            patch("os.path.isdir", return_value=False),
            patch("glob.glob", return_value=[]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                return_value=(True, "downloaded"),
            ) as mock_download_one,
            patch("uuid.uuid4") as mock_uuid4,
        ):
            mock_uuid4.return_value.hex = _FIXED_UUID_HEX
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            # normalize_key strips leading "/"; Path normalises "./" prefix
            expected_local = Path(_EXPECTED_TMP) / "data/test.parquet"
            mock_download_one.assert_called_once_with(
                _MOCK_STORE,
                "data/test.parquet",
                expected_local,
                skip_if_exists=False,
            )
            assert result == [str(expected_local)]

    @pytest.mark.asyncio
    async def test__download_files_download_directory_success(self):
        """Test successful download of directory from object store."""
        path = "/data"
        input_instance = MockReader(path)
        store_keys = ["data/file1.parquet", "data/file2.parquet"]

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=[]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._list_keys",
                new_callable=AsyncMock,
                return_value=store_keys,
            ) as mock_list_keys,
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                return_value=(True, "downloaded"),
            ) as mock_download_one,
            patch("uuid.uuid4") as mock_uuid4,
        ):
            mock_uuid4.return_value.hex = _FIXED_UUID_HEX
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            mock_list_keys.assert_called_once_with(
                "data", _MOCK_STORE, suffix=".parquet", normalize=False
            )
            assert mock_download_one.call_count == 2
            expected_files = [str(Path(_EXPECTED_TMP) / k) for k in store_keys]
            assert result == expected_files

    @pytest.mark.asyncio
    async def test__download_files_download_specific_files_success(self):
        """Test successful download of specific files from object store."""
        path = "/data"
        file_names = ["file1.parquet", "file2.parquet"]
        input_instance = MockReader(path, file_names)
        expected_files = [
            str(Path(_EXPECTED_TMP) / "data/file1.parquet"),
            str(Path(_EXPECTED_TMP) / "data/file2.parquet"),
        ]

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", side_effect=[[]]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                return_value=(True, "downloaded"),
            ) as mock_download_one,
            patch("uuid.uuid4") as mock_uuid4,
        ):
            mock_uuid4.return_value.hex = _FIXED_UUID_HEX
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            assert mock_download_one.call_count == 2
            mock_download_one.assert_any_call(
                _MOCK_STORE,
                "data/file1.parquet",
                Path(_EXPECTED_TMP) / "data/file1.parquet",
                skip_if_exists=False,
            )
            mock_download_one.assert_any_call(
                _MOCK_STORE,
                "data/file2.parquet",
                Path(_EXPECTED_TMP) / "data/file2.parquet",
                skip_if_exists=False,
            )
            assert result == expected_files

    @pytest.mark.asyncio
    async def test__download_files_download_failure(self):
        """Test download failure from object store."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=False),
            patch("glob.glob", return_value=[]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                side_effect=Exception("Download failed"),
            ),
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await _download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

    @pytest.mark.asyncio
    async def test__download_files_download_success_but_no_files_found(self):
        """Test download succeeds but no matching keys found in object store."""
        path = "/data"
        input_instance = MockReader(path)

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=[]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._list_keys",
                new_callable=AsyncMock,
                return_value=[],  # No matching keys in store
            ),
        ):
            with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
                await _download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

    @pytest.mark.asyncio
    async def test__download_files_recursive_glob_pattern(self):
        """Test that recursive glob pattern is used for directory search."""
        path = "/data"
        input_instance = MockReader(path)
        expected_files = ["/data/subdir/file1.parquet", "/data/file2.parquet"]

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=expected_files) as mock_glob,
        ):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            # Should use recursive glob pattern (OS-specific path separators)
            expected_pattern = os.path.join("/data", "**", "*.parquet")
            mock_glob.assert_called_once_with(expected_pattern, recursive=True)
            assert result == expected_files

    @pytest.mark.asyncio
    async def test__download_files_file_extension_filtering(self):
        """Test that only files with correct extension are returned."""
        path = "/data"
        input_instance = MockReader(path)
        expected_files = ["/data/file1.parquet", "/data/file3.parquet"]

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=expected_files),
        ):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            assert result == expected_files

    @pytest.mark.asyncio
    async def test__download_files_file_names_basename_matching(self):
        """Test file_names matching works with both full path and basename."""
        path = "/data"
        file_names = ["file1.parquet"]  # Just basename
        input_instance = MockReader(path, file_names)
        all_files = ["/data/subdir/file1.parquet", "/data/file2.parquet"]
        expected_files = ["/data/subdir/file1.parquet"]

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch("glob.glob", return_value=all_files),
        ):
            result = await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            assert result == expected_files

    @pytest.mark.asyncio
    async def test__download_files_logging_messages(self):
        """Test that appropriate logging messages are generated."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with (
            patch("os.path.isfile", return_value=True),
            patch("application_sdk.storage.formats.utils.logger") as mock_logger,
        ):
            await _download_files(
                input_instance.path, ".parquet", input_instance.file_names
            )

            mock_logger.info.assert_called_with(
                "Found %d %s files locally at %s",
                1,
                ".parquet",
                "/data/test.parquet",
            )

    @pytest.mark.asyncio
    async def test__download_files_logging_download_attempt(self):
        """Test logging when attempting download from object store."""
        path = "/data/test.parquet"
        input_instance = MockReader(path)

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=False),
            patch("glob.glob", return_value=[]),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                side_effect=Exception("Download failed"),
            ),
            patch("application_sdk.storage.formats.utils.logger") as mock_logger,
        ):
            with pytest.raises(SDKIOError):
                await _download_files(
                    input_instance.path, ".parquet", input_instance.file_names
                )

            mock_logger.info.assert_any_call(
                "No local %s files found at '%s', checking object store",
                ".parquet",
                "/data/test.parquet",
            )


class TestDownloadFilesIsolation:
    """Regression tests for the parallel download race condition.

    The bug: concurrent transform_data activities all download to
    ./local/tmp/ and overwrite each other's files. The fix uses a
    UUID-isolated subdirectory per _download_files() call.
    """

    @pytest.mark.asyncio
    async def test_concurrent_downloads_get_isolated_directories(self):
        """Two concurrent _download_files calls must use DIFFERENT temp dirs."""
        import asyncio

        path = "/raw/table"

        with (
            patch("os.path.isfile", return_value=False),
            patch("os.path.isdir", return_value=True),
            patch(
                "application_sdk.storage.formats.utils._resolve_store",
                return_value=_MOCK_STORE,
            ),
            patch(
                "application_sdk.storage.formats.utils._list_keys",
                new_callable=AsyncMock,
                return_value=["raw/table/file.parquet"],
            ),
            patch(
                "application_sdk.storage.formats.utils._download_one",
                new_callable=AsyncMock,
                return_value=(True, "downloaded"),
            ) as mock_download_one,
        ):
            results = await asyncio.gather(
                _download_files(path, ".parquet"),
                _download_files(path, ".parquet"),
            )

            assert len(results) == 2
            assert mock_download_one.call_count == 2

            # _download_one(store, key, local_file, *, skip_if_exists)
            # local_file is the 3rd positional arg — extract its parent (isolated_tmp)
            local1: Path = mock_download_one.call_args_list[0][0][2]
            local2: Path = mock_download_one.call_args_list[1][0][2]

            # The UUID segment sits two levels above the file: local/tmp/<uuid>/raw/table/file.parquet
            tmp1 = str(local1.parents[2])
            tmp2 = str(local2.parents[2])

            assert tmp1 != tmp2, f"Concurrent downloads used same destination: {tmp1}"
            assert tmp1.startswith("local/tmp/")
            assert tmp2.startswith("local/tmp/")

    @pytest.mark.asyncio
    async def test_file_names_with_relative_path_match_correctly(self):
        """Reproduce the basename collision bug.

        Before fix: file_names=["table/chunk-0.parquet"] matched ANY file
        named chunk-0.parquet regardless of directory.
        After fix: matching uses relative paths.
        """
        with tempfile.TemporaryDirectory() as tmp:
            table_dir = Path(tmp) / "table"
            schema_dir = Path(tmp) / "schema"
            table_dir.mkdir()
            schema_dir.mkdir()

            table_chunk = table_dir / "chunk-0-part0.parquet"
            schema_chunk = schema_dir / "chunk-0-part0.parquet"
            table_chunk.write_bytes(b"table data")
            schema_chunk.write_bytes(b"schema data")

            # Activity A wants only table/chunk-0-part0.parquet
            result = find_local_files_by_extension(
                tmp, ".parquet", file_names=["table/chunk-0-part0.parquet"]
            )
            assert len(result) == 1
            assert str(table_chunk) in result
            assert str(schema_chunk) not in result

            # Activity B wants only schema/chunk-0-part0.parquet
            result_b = find_local_files_by_extension(
                tmp, ".parquet", file_names=["schema/chunk-0-part0.parquet"]
            )
            assert len(result_b) == 1
            assert str(schema_chunk) in result_b
            assert str(table_chunk) not in result_b
