from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import orjson
import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.common.types import DataframeType
from application_sdk.storage.formats.json import JsonFileReader
from application_sdk.storage.formats.utils import _download_files
from application_sdk.testing.hypothesis.strategies.inputs.json_input import (
    json_input_config_strategy,
)

_MOCK_STORE = MagicMock()
_FIXED_HEX = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
_DL_ID = _FIXED_HEX[:12]
_EXPECTED_TMP = str(Path("./local/tmp/") / _DL_ID)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "json_input_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("json_input_tests")


@given(config=json_input_config_strategy)
def test_init(config: dict[str, Any]) -> None:
    json_input = JsonFileReader(
        path=config["path"],
        file_names=config["file_names"],
    )

    assert json_input.path.endswith(config["path"])
    assert json_input.file_names == config["file_names"]


def test_init_single_file_with_file_names_raises_error() -> None:
    """Test that JsonFileReader raises when single file path is combined with file_names."""
    from application_sdk.storage.formats.format_errors import (
        SingleFilePathWithFileNamesError,
    )

    with pytest.raises(SingleFilePathWithFileNamesError) as exc_info:
        JsonFileReader(path="/data/test.json", file_names=["other.json"])
    assert exc_info.value.code == "INVALID_INPUT_FORMAT_SINGLE_PATH_WITH_FILE_NAMES"


@pytest.mark.asyncio
async def test_not_download_file_that_exists() -> None:
    """Test that no download occurs when a JSON file exists locally."""
    path = "/data/test.json"

    with (
        patch("os.path.isfile", return_value=True),
        patch("os.path.isdir", return_value=False),
        patch(
            "application_sdk.storage.formats.utils._download_one",
            new_callable=AsyncMock,
        ) as mock_download_one,
    ):
        json_input = JsonFileReader(path=path)

        result = await _download_files(json_input.path, ".json", json_input.file_names)
        mock_download_one.assert_not_called()
        assert result == [path]


@pytest.mark.asyncio
async def test_download_file_invoked_for_missing_files() -> None:
    """Ensure that a download is triggered when the file does not exist locally."""
    path = "/local"
    file_names = ["a.json", "b.json"]

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
        mock_uuid4.return_value.hex = _FIXED_HEX
        json_input = JsonFileReader(
            path=path, file_names=file_names, dataframe_type=DataframeType.pandas
        )

        result = await _download_files(json_input.path, ".json", json_input.file_names)

        mock_download_one.assert_has_calls(
            [
                call(
                    _MOCK_STORE,
                    "local/a.json",
                    Path(_EXPECTED_TMP) / "local/a.json",
                    skip_if_exists=False,
                ),
                call(
                    _MOCK_STORE,
                    "local/b.json",
                    Path(_EXPECTED_TMP) / "local/b.json",
                    skip_if_exists=False,
                ),
            ],
            any_order=True,
        )
        assert result == [
            str(Path(_EXPECTED_TMP) / "local/a.json"),
            str(Path(_EXPECTED_TMP) / "local/b.json"),
        ]


@pytest.mark.asyncio
async def test_download_file_not_invoked_when_file_present() -> None:
    """Ensure no download occurs when the file already exists locally."""
    path = "/local"
    file_names = ["exists.json"]

    with (
        patch("os.path.isfile", return_value=False),
        patch("os.path.isdir", return_value=True),
        patch("glob.glob", return_value=["/local/exists.json"]),
        patch(
            "application_sdk.storage.formats.utils._download_one",
            new_callable=AsyncMock,
        ) as mock_download_one,
    ):
        json_input = JsonFileReader(
            path=path, file_names=file_names, dataframe_type=DataframeType.pandas
        )

        result = await _download_files(json_input.path, ".json", json_input.file_names)

        mock_download_one.assert_not_called()
        assert result == ["/local/exists.json"]


@pytest.mark.asyncio
async def test_download_file_error_propagation() -> None:
    """Ensure errors during download are surfaced as ObjectStoreDownloadError."""
    from application_sdk.storage.formats.format_errors import ObjectStoreDownloadError

    path = "/local"
    file_names = ["bad.json"]

    with (
        patch("os.path.isfile", return_value=False),
        patch("os.path.isdir", return_value=True),
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
        json_input = JsonFileReader(
            path=path, file_names=file_names, dataframe_type=DataframeType.pandas
        )

        with pytest.raises(ObjectStoreDownloadError) as exc_info:
            await _download_files(json_input.path, ".json", json_input.file_names)
        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_DOWNLOAD"


@pytest.mark.asyncio
async def test_no_matching_files_surfaces_read_error_not_download_error() -> None:
    """Empty prefix listing must raise ObjectStoreReadError, not ObjectStoreDownloadError.

    Regression guard for the classification bug where the broad ``except Exception``
    around the prefix-listing path swallowed the typed ``ObjectStoreReadError`` and
    re-wrapped it as ``ObjectStoreDownloadError`` — producing the mismatched
    ``DEPENDENCY_UNAVAILABLE_OBJECT_STORE_DOWNLOAD`` code + "Downloaded but no
    matching files found" message seen in production.
    """
    from application_sdk.storage.formats.format_errors import (
        ObjectStoreDownloadError,
        ObjectStoreReadError,
    )

    path = "/local"

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
            return_value=[],
        ),
    ):
        with pytest.raises(ObjectStoreReadError) as exc_info:
            await _download_files(path, ".json", None)
        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_READ"
        assert not isinstance(exc_info.value, ObjectStoreDownloadError)
        # Operator-visible fields: prefix + extension must round-trip so the
        # alert tells you exactly what was searched.
        assert exc_info.value.path == path
        assert exc_info.value.file_extension == ".json"
        # `message` states what happened; `suggested_action` states what to
        # do -- kept separate per the AppError contract.
        assert exc_info.value.suggested_action is not None
        assert "upstream" in exc_info.value.suggested_action.lower()


# ---------------------------------------------------------------------------
# Compat shim tests -- DataframeType.daft
# ---------------------------------------------------------------------------


def test_dataframe_type_daft_emits_deprecation_warning() -> None:
    """Passing DataframeType.daft must emit DeprecationWarning and route to pandas."""
    with pytest.warns(DeprecationWarning, match="DataframeType.daft is deprecated"):
        reader = JsonFileReader(
            path="/data",
            dataframe_type=DataframeType.daft,
        )
    assert reader.dataframe_type == DataframeType.pandas


# ---------------------------------------------------------------------------
# orjson-backed batched read tests
# ---------------------------------------------------------------------------


def _write_json_file(tmp_path: Path, name: str, records: list[dict]) -> str:
    """Write newline-delimited JSON and return its path."""
    path = tmp_path / name
    with open(path, "wb") as f:
        f.writelines(orjson.dumps(record) + b"\n" for record in records)
    return str(path)


@pytest.mark.asyncio
async def test_read_batches_yields_dataframes(tmp_path: Path) -> None:
    """_get_batched_dataframe yields pd.DataFrame batches via orjson line reading."""
    import pandas as pd

    records = [{"id": i, "name": f"item-{i}"} for i in range(5)]
    json_file = _write_json_file(tmp_path, "data.json", records)

    async def dummy_download(path, file_extension, file_names=None):
        return [json_file]

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        reader = JsonFileReader(
            path=str(tmp_path), chunk_size=100000, dataframe_type=DataframeType.pandas
        )
        batches = reader.read_batches()
        chunks = [chunk async for chunk in batches]

    assert len(chunks) == 1
    assert isinstance(chunks[0], pd.DataFrame)
    assert list(chunks[0]["id"]) == [r["id"] for r in records]
    assert list(chunks[0]["name"]) == [r["name"] for r in records]


@pytest.mark.asyncio
async def test_read_batches_respects_chunk_size(tmp_path: Path) -> None:
    """read_batches splits records into batches of chunk_size."""
    records = [{"id": i} for i in range(10)]
    json_file = _write_json_file(tmp_path, "data.json", records)

    async def dummy_download(path, file_extension, file_names=None):
        return [json_file]

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        reader = JsonFileReader(
            path=str(tmp_path), chunk_size=3, dataframe_type=DataframeType.pandas
        )
        batches = reader.read_batches()
        chunks = [chunk async for chunk in batches]

    # 10 records / chunk_size 3 -> 3 full batches + 1 remainder
    assert len(chunks) == 4
    assert len(chunks[0]) == 3
    assert len(chunks[-1]) == 1
    total = sum(len(c) for c in chunks)
    assert total == 10


@pytest.mark.asyncio
async def test_read_batches_empty_file_list(tmp_path: Path) -> None:
    """An empty file list should result in no yielded batches."""

    async def dummy_download(path, file_extension, file_names=None):
        return []

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        json_input = JsonFileReader(
            path="/data", file_names=[], dataframe_type=DataframeType.pandas
        )

        batches_result = json_input.read_batches()
        batches = [chunk async for chunk in batches_result]

    assert batches == []


@pytest.mark.asyncio
async def test_read_batches_multiple_files(tmp_path: Path) -> None:
    """read_batches concatenates records across multiple files."""
    f1 = _write_json_file(tmp_path, "f1.json", [{"id": 1}, {"id": 2}])
    f2 = _write_json_file(tmp_path, "f2.json", [{"id": 3}])

    async def dummy_download(path, file_extension, file_names=None):
        return [f1, f2]

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        reader = JsonFileReader(
            path=str(tmp_path),
            chunk_size=100000,
            dataframe_type=DataframeType.pandas,
        )
        batches = reader.read_batches()
        chunks = [chunk async for chunk in batches]

    total = sum(len(c) for c in chunks)
    assert total == 3


# ---------------------------------------------------------------------------
# orjson-backed non-batched read test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_pandas_dataframe(tmp_path: Path) -> None:
    """read() returns a pandas DataFrame built from orjson-decoded records."""
    import pandas as pd

    records = [{"id": i, "val": f"v{i}"} for i in range(4)]
    json_file = _write_json_file(tmp_path, "data.json", records)

    async def dummy_download(path, file_extension, file_names=None):
        return [json_file]

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        reader = JsonFileReader(path=str(tmp_path), dataframe_type=DataframeType.pandas)
        result = await reader.read()

    assert isinstance(result, pd.DataFrame)
    assert len(result) == 4
    assert list(result.columns) == ["id", "val"]


# ---------------------------------------------------------------------------
# Context Manager and Close Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_calls_close(tmp_path: Path) -> None:
    """Verify that using async with calls close() on exit."""
    json_file = _write_json_file(tmp_path, "test.json", [{"id": 1}])

    async def dummy_download(path, file_extension, file_names=None):
        return [json_file]

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        async with JsonFileReader(
            path=str(tmp_path), dataframe_type=DataframeType.pandas
        ) as reader:
            await reader.read()
            assert not reader._is_closed

    # After exiting context, reader should be closed
    assert reader._is_closed


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """Verify that calling close() multiple times is safe."""
    path = "/data"
    reader = JsonFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Close multiple times - should not raise
    await reader.close()
    assert reader._is_closed

    await reader.close()  # Should be a no-op
    assert reader._is_closed

    await reader.close()  # Should still be a no-op
    assert reader._is_closed


@pytest.mark.asyncio
async def test_read_after_close_raises_error(tmp_path: Path) -> None:
    """Verify that reading after close raises ReaderClosedError."""
    json_file = _write_json_file(tmp_path, "test.json", [{"id": 1}])

    async def dummy_download(path, file_extension, file_names=None):
        return [json_file]

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path)
        reader = JsonFileReader(path=path, dataframe_type=DataframeType.pandas)

        # Read should work before close
        await reader.read()

        # Close the reader
        await reader.close()

    # Read should raise after close
    from application_sdk.storage.formats.format_errors import ReaderClosedError

    with pytest.raises(ReaderClosedError) as exc_info:
        await reader.read()
    assert exc_info.value.code == "PRECONDITION_FORMAT_READER_CLOSED"


@pytest.mark.asyncio
async def test_read_batches_after_close_raises_error() -> None:
    """Verify that read_batches after close raises ReaderClosedError."""
    from application_sdk.storage.formats.format_errors import ReaderClosedError

    path = "/data"
    reader = JsonFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Close the reader
    await reader.close()

    # read_batches should raise after close
    with pytest.raises(ReaderClosedError) as exc_info:
        reader.read_batches()
    assert exc_info.value.code == "PRECONDITION_FORMAT_READER_CLOSED"


@pytest.mark.asyncio
async def test_cleanup_on_close_default_true() -> None:
    """Verify that cleanup_on_close defaults to True."""
    path = "/data"
    reader = JsonFileReader(path=path, dataframe_type=DataframeType.pandas)

    assert reader.cleanup_on_close is True


@pytest.mark.asyncio
async def test_cleanup_on_close_false_retains_files(tmp_path: Path) -> None:
    """Verify that setting cleanup_on_close=False retains downloaded files."""
    json_file = _write_json_file(tmp_path, "test.json", [{"id": 1}])

    downloaded_files = [json_file]

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path)
        reader = JsonFileReader(
            path=path,
            dataframe_type=DataframeType.pandas,
            cleanup_on_close=False,
        )

        # Read to trigger download tracking
        await reader.read()

        # Verify files are tracked
        assert reader._downloaded_files == downloaded_files

        # Mock cleanup to track if it's called
        cleanup_called = False

        async def mock_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        reader._cleanup_downloaded_files = mock_cleanup  # type: ignore[method-assign]

        # Close should NOT call cleanup when cleanup_on_close=False
        await reader.close()

    assert not cleanup_called
    assert reader._is_closed


@pytest.mark.asyncio
async def test_cleanup_on_close_true_cleans_files(tmp_path: Path) -> None:
    """Verify that setting cleanup_on_close=True cleans up downloaded files."""
    json_file = _write_json_file(tmp_path, "test.json", [{"id": 1}])

    downloaded_files = [json_file]

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path)
        reader = JsonFileReader(
            path=path,
            dataframe_type=DataframeType.pandas,
            cleanup_on_close=True,
        )

        # Read to trigger download tracking
        await reader.read()

        # Verify files are tracked
        assert reader._downloaded_files == downloaded_files

        # Mock cleanup to track if it's called
        cleanup_called = False

        async def mock_cleanup():
            nonlocal cleanup_called
            cleanup_called = True
            reader._downloaded_files.clear()

        reader._cleanup_downloaded_files = mock_cleanup  # type: ignore[method-assign]

        # Close should call cleanup when cleanup_on_close=True
        await reader.close()

    assert cleanup_called
    assert reader._is_closed


@pytest.mark.asyncio
async def test_downloaded_files_tracked_on_read(tmp_path: Path) -> None:
    """Verify that downloaded files are tracked when read() is called."""
    json_file = _write_json_file(tmp_path, "test.json", [{"id": 1}])

    downloaded_files = [json_file]

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path)
        reader = JsonFileReader(path=path, dataframe_type=DataframeType.pandas)

        # Initially no downloaded files
        assert reader._downloaded_files == []

        # Read to trigger download
        await reader.read()

        # Files should now be tracked
        assert reader._downloaded_files == downloaded_files


@pytest.mark.asyncio
async def test_read_no_files(tmp_path: Path) -> None:
    """read() with no files available returns an empty pandas DataFrame."""
    import pandas as pd

    async def dummy_download(path, file_extension, file_names=None):
        return []

    with patch(
        "application_sdk.storage.formats.json._download_files",
        side_effect=dummy_download,
    ):
        reader = JsonFileReader(path=str(tmp_path), dataframe_type=DataframeType.pandas)
        result = await reader.read()

    assert isinstance(result, pd.DataFrame)
    assert len(result) == 0
