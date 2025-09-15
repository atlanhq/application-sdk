# Added os import for path manipulations used in new tests
import os
from typing import Any, Dict
from unittest.mock import call, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.inputs.json import JsonInput
from application_sdk.test_utils.hypothesis.strategies.inputs.json_input import (
    json_input_config_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "json_input_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("json_input_tests")


@given(config=json_input_config_strategy)
def test_init(config: Dict[str, Any]) -> None:
    json_input = JsonInput(
        path=config["path"],
        file_names=config["file_names"],
    )

    assert json_input.path.endswith(config["path"])
    assert json_input.file_names == config["file_names"]


def test_init_single_file_with_file_names_raises_error() -> None:
    """Test that JsonInput raises ValueError when single file path is combined with file_names."""
    with pytest.raises(ValueError, match="Cannot specify both a single file path"):
        JsonInput(path="/data/test.json", file_names=["other.json"])


@pytest.mark.asyncio
async def test_not_download_file_that_exists() -> None:
    """Test that no download occurs when a JSON file exists locally."""
    path = "/data/test.json"  # Path with correct extension
    # Don't use file_names with single file path due to validation

    with patch("os.path.isfile", return_value=True), patch(
        "os.path.isdir", return_value=False
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        json_input = JsonInput(path=path)  # No file_names

        result = await json_input.download_files()
        mock_download.assert_not_called()
        assert result == [path]  # Should return the local file


@pytest.mark.asyncio
async def test_download_file_invoked_for_missing_files() -> None:
    """Ensure that a download is triggered when the file does not exist locally."""
    path = "/local"
    file_names = ["a.json", "b.json"]

    def mock_isfile(path):
        # Return False for initial local check, True for downloaded files
        if path in ["./local/tmp/local/a.json", "./local/tmp/local/b.json"]:
            return True
        return False

    with patch("os.path.isfile", side_effect=mock_isfile), patch(
        "os.path.isdir", return_value=True
    ), patch("glob.glob", side_effect=[[]]), patch(  # Only for initial local check
        "application_sdk.inputs.get_object_store_prefix",
        side_effect=lambda p: p.lstrip("/").replace("\\", "/"),
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        json_input = JsonInput(path=path, file_names=file_names)

        result = await json_input.download_files()

        # Each file should be attempted to be downloaded - using correct signature (with destination)
        expected_calls = [
            call(source="local/a.json", destination="./local/tmp/local/a.json"),
            call(source="local/b.json", destination="./local/tmp/local/b.json"),
        ]
        mock_download.assert_has_calls(expected_calls, any_order=True)
        assert result == ["./local/tmp/local/a.json", "./local/tmp/local/b.json"]


@pytest.mark.asyncio
async def test_download_file_not_invoked_when_file_present() -> None:
    """Ensure no download occurs when the file already exists locally."""
    path = "/local"
    file_names = ["exists.json"]

    with patch("os.path.isfile", return_value=False), patch(
        "os.path.isdir", return_value=True
    ), patch("glob.glob", return_value=["/local/exists.json"]), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        json_input = JsonInput(path=path, file_names=file_names)

        result = await json_input.download_files()

        mock_download.assert_not_called()
        assert result == ["/local/exists.json"]


@pytest.mark.asyncio
async def test_download_file_error_propagation() -> None:
    """Ensure errors during download are surfaced as application_sdk IOError."""
    from application_sdk.common.error_codes import IOError as SDKIOError

    path = "/local"
    file_names = ["bad.json"]

    # Mock no local files found, then download failure
    with patch("os.path.isfile", return_value=False), patch(
        "os.path.isdir", return_value=True
    ), patch("glob.glob", return_value=[]), patch(
        "application_sdk.inputs.get_object_store_prefix",
        side_effect=lambda p: p.lstrip("/").replace("\\", "/"),
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file",
        side_effect=Exception("Download failed"),
    ):
        json_input = JsonInput(path=path, file_names=file_names)

        with pytest.raises(SDKIOError, match="ATLAN-IO-503-00"):
            await json_input.download_files()


# ---------------------------------------------------------------------------
# Pandas-related helpers & tests
# ---------------------------------------------------------------------------


# Helper to install dummy pandas module and capture read_json invocations
def _install_dummy_pandas(monkeypatch):
    """Install a dummy pandas module in sys.modules that tracks calls to read_json."""
    import os
    import sys
    import types

    dummy_pandas = types.ModuleType("pandas")
    call_log: list[dict] = []

    def read_json(path, chunksize=None, lines=None):  # noqa: D401, ANN001
        call_log.append({"path": path, "chunksize": chunksize, "lines": lines})
        # Return two synthetic chunks for iteration
        return [f"chunk1-{os.path.basename(path)}", f"chunk2-{os.path.basename(path)}"]

    def concat(objs, ignore_index=None):  # noqa: D401, ANN001
        return "combined:" + ",".join(objs)

    dummy_pandas.read_json = read_json  # type: ignore[attr-defined]
    dummy_pandas.concat = concat  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pandas", dummy_pandas)

    return call_log


@pytest.mark.asyncio
async def test_get_batched_dataframe_with_mocked_pandas(monkeypatch) -> None:
    """Verify that get_batched_dataframe streams chunks and respects chunk_size."""

    file_names = ["abc.json"]
    path = "/data"

    expected_chunksize = 5
    call_log = _install_dummy_pandas(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return (
            [os.path.join(self.path, fn) for fn in self.file_names]
            if hasattr(self, "file_names") and self.file_names
            else []
        )

    # Mock the base Input class method since JsonInput calls super().download_files()
    from application_sdk.inputs import Input

    monkeypatch.setattr(Input, "download_files", dummy_download, raising=False)

    json_input = JsonInput(
        path=path, file_names=file_names, chunk_size=expected_chunksize
    )

    chunks = [chunk async for chunk in json_input.get_batched_dataframe()]

    # Two chunks per file as defined in dummy pandas implementation
    assert chunks == ["chunk1-abc.json", "chunk2-abc.json"]

    # Confirm read_json was invoked with correct args
    assert call_log == [
        {
            "path": os.path.join(path, "abc.json"),
            "chunksize": expected_chunksize,
            "lines": True,
        }
    ]


@pytest.mark.asyncio
async def test_get_batched_dataframe_empty_file_list(monkeypatch) -> None:
    """An empty file list should result in no yielded batches."""

    call_log = _install_dummy_pandas(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return []

    # Mock the base Input class method since JsonInput calls super().download_files()
    from application_sdk.inputs import Input

    monkeypatch.setattr(Input, "download_files", dummy_download, raising=False)

    json_input = JsonInput(path="/data", file_names=[])

    batches = [chunk async for chunk in json_input.get_batched_dataframe()]

    assert batches == []
    # No pandas.read_json calls should have been made
    assert call_log == []


# ---------------------------------------------------------------------------
# Daft-related helpers & tests
# ---------------------------------------------------------------------------


def _install_dummy_daft(monkeypatch):  # noqa: D401, ANN001
    import sys
    import types

    dummy_daft = types.ModuleType("daft")
    call_log: list[dict] = []

    def read_json(path, _chunk_size=None):  # noqa: D401, ANN001
        call_log.append({"path": path, "_chunk_size": _chunk_size})
        return f"daft_df:{path}"

    dummy_daft.read_json = read_json  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "daft", dummy_daft)

    return call_log


@pytest.mark.asyncio
async def test_get_daft_dataframe(monkeypatch) -> None:
    """Verify that get_daft_dataframe merges path correctly and delegates to daft.read_json."""

    call_log = _install_dummy_daft(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return (
            [
                os.path.join(self.path, fn).replace(os.path.sep, "/")
                for fn in self.file_names
            ]
            if hasattr(self, "file_names") and self.file_names
            else []
        )

    # Mock the base Input class method since JsonInput calls super().download_files()
    from application_sdk.inputs import Input

    monkeypatch.setattr(Input, "download_files", dummy_download, raising=False)

    path = "/tmp"
    file_names = ["dir/file1.json", "dir/file2.json"]

    json_input = JsonInput(path=path, file_names=file_names)

    result = await json_input.get_daft_dataframe()

    expected_files = ["/tmp/dir/file1.json", "/tmp/dir/file2.json"]

    assert result == f"daft_df:{expected_files}"
    assert call_log == [{"path": expected_files, "_chunk_size": None}]


@pytest.mark.asyncio
async def test_get_daft_dataframe_no_files(monkeypatch) -> None:
    """Calling get_daft_dataframe without files should return empty result."""

    call_log = _install_dummy_daft(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return []  # Return empty list when no files found

    # Mock the base Input class method since JsonInput calls super().download_files()
    from application_sdk.inputs import Input

    monkeypatch.setattr(Input, "download_files", dummy_download, raising=False)

    json_input = JsonInput(path="/tmp", file_names=[])

    result = await json_input.get_daft_dataframe()

    # Should return empty daft result
    assert result == "daft_df:[]"
    assert call_log == [{"path": [], "_chunk_size": None}]


@pytest.mark.asyncio
async def test_get_batched_daft_dataframe(monkeypatch) -> None:
    """Ensure get_batched_daft_dataframe yields a frame per file and passes chunk size."""

    call_log = _install_dummy_daft(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return (
            [os.path.join(self.path, fn) for fn in self.file_names]
            if hasattr(self, "file_names") and self.file_names
            else []
        )

    # Mock the base Input class method since JsonInput calls super().download_files()
    from application_sdk.inputs import Input

    monkeypatch.setattr(Input, "download_files", dummy_download, raising=False)

    path = "/data"
    file_names = ["one.json", "two.json"]

    json_input = JsonInput(path=path, file_names=file_names, chunk_size=123)

    frames = [frame async for frame in json_input.get_batched_daft_dataframe()]

    expected_frames = [f"daft_df:{os.path.join(path, fn)}" for fn in file_names]

    assert frames == expected_frames

    # Ensure a call was logged per file with the correct chunk size
    assert call_log == [
        {"path": os.path.join(path, "one.json"), "_chunk_size": 123},
        {"path": os.path.join(path, "two.json"), "_chunk_size": 123},
    ]
