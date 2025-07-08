# Added os import for path manipulations used in new tests
import os
from typing import Any, Dict
from unittest.mock import call, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.inputs.json import JsonInput
from application_sdk.test_utils.hypothesis.strategies.inputs.json_input import (
    download_prefix_strategy,
    file_names_strategy,
    json_input_config_strategy,
    safe_path_strategy,
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
        download_file_prefix=config["download_file_prefix"],
        file_names=config["file_names"],
    )

    assert json_input.path.endswith(config["path"])
    assert json_input.download_file_prefix == config["download_file_prefix"]
    assert json_input.file_names == config["file_names"]


@pytest.mark.asyncio
@given(
    path=safe_path_strategy,
    prefix=download_prefix_strategy,
    file_names=file_names_strategy,
)
async def test_not_download_file_that_exists(
    path: str, prefix: str, file_names: list[str]
) -> None:
    with patch("os.path.exists") as mock_exists, patch(
        "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
    ) as mock_download:
        mock_exists.return_value = True
        json_input = JsonInput(
            path=path, download_file_prefix=prefix, file_names=file_names
        )

        await json_input.download_files()
        mock_download.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_invoked_for_missing_files() -> None:
    """Ensure that a download is triggered when the file does not exist locally."""
    path = "/local"
    prefix = "remote"
    file_names = ["a.json", "b.json"]

    with patch("os.path.exists", return_value=False) as mock_exists, patch(
        "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
    ) as mock_download:
        json_input = JsonInput(
            path=path, download_file_prefix=prefix, file_names=file_names
        )

        await json_input.download_files()

        # One os.path.exists call per file
        assert mock_exists.call_count == len(file_names)

        # Each file should be attempted to be downloaded
        expected_calls = [
            call(os.path.join(prefix, file_name), os.path.join(path, file_name))
            for file_name in file_names
        ]
        mock_download.assert_has_calls(expected_calls, any_order=True)


@pytest.mark.asyncio
async def test_download_file_not_invoked_when_file_present() -> None:
    """Ensure no download occurs when the file already exists locally."""
    path = "/local"
    prefix = "remote"
    file_names = ["exists.json"]

    with patch("os.path.exists", return_value=True) as _, patch(
        "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
    ) as mock_download:
        json_input = JsonInput(
            path=path, download_file_prefix=prefix, file_names=file_names
        )

        await json_input.download_files()

        mock_download.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_error_propagation() -> None:
    """Ensure errors during download are surfaced as application_sdk IOError."""
    from application_sdk.common.error_codes import IOError as SDKIOError

    path = "/local"
    prefix = "remote"
    file_names = ["bad.json"]

    # Mock exists -> False so download attempted
    with patch("os.path.exists", return_value=False), patch(
        "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store",
        side_effect=SDKIOError("boom"),
    ):
        json_input = JsonInput(
            path=path, download_file_prefix=prefix, file_names=file_names
        )

        with pytest.raises(SDKIOError):
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
        return None

    monkeypatch.setattr(JsonInput, "download_files", dummy_download, raising=False)

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
        return None

    monkeypatch.setattr(JsonInput, "download_files", dummy_download, raising=False)

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
        return None

    monkeypatch.setattr(JsonInput, "download_files", dummy_download, raising=False)

    path = "/tmp"
    file_names = ["dir/file1.json", "dir/file2.json"]

    json_input = JsonInput(path=path, file_names=file_names)

    result = await json_input.get_daft_dataframe()

    expected_directory = os.path.join(path, "dir")

    assert result == f"daft_df:{expected_directory}/*.json"
    assert call_log == [{"path": f"{expected_directory}/*.json", "_chunk_size": None}]


@pytest.mark.asyncio
async def test_get_daft_dataframe_no_files(monkeypatch) -> None:
    """Calling get_daft_dataframe without files should raise ValueError."""

    _install_dummy_daft(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return None

    monkeypatch.setattr(JsonInput, "download_files", dummy_download, raising=False)

    json_input = JsonInput(path="/tmp", file_names=[])

    with pytest.raises(ValueError):
        await json_input.get_daft_dataframe()


@pytest.mark.asyncio
async def test_get_batched_daft_dataframe(monkeypatch) -> None:
    """Ensure get_batched_daft_dataframe yields a frame per file and passes chunk size."""

    call_log = _install_dummy_daft(monkeypatch)

    async def dummy_download(self):  # noqa: D401, ANN001
        return None

    monkeypatch.setattr(JsonInput, "download_files", dummy_download, raising=False)

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
