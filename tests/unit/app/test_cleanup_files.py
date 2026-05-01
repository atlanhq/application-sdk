"""Tests for App.cleanup_files() framework task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, _app_state, _app_state_lock
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import CleanupInput, CleanupOutput
from application_sdk.contracts.types import FileReference


@dataclass
class _CFInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _CFOutput(Output, allow_unbounded_fields=True):
    result: str = ""


class _CleanupApp(App):
    async def run(self, input: _CFInput) -> _CFOutput:
        return _CFOutput()


class TestCleanupFiles:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        with _app_state_lock:
            _app_state.clear()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        with _app_state_lock:
            _app_state.clear()

    @pytest.mark.asyncio
    async def test_removes_tracked_file_ref_local_paths(self, tmp_path: Any) -> None:
        f = tmp_path / "output.parquet"
        f.write_text("data")
        sidecar = tmp_path / "output.parquet.sha256"
        sidecar.write_text("abc123")

        ref = FileReference(local_path=str(f), storage_path="artifacts/x")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get",
            return_value={ref},
        ):
            # extra_paths avoids calling build_output_path (no activity context)
            result = await app.cleanup_files(
                CleanupInput(extra_paths=["nonexistent-dir"])
            )

        assert not f.exists()
        assert not sidecar.exists()
        assert result.path_results[str(f)] is True
        assert result.path_results[str(f) + ".sha256"] is True

    @pytest.mark.asyncio
    async def test_ref_without_local_path_is_skipped(self, tmp_path: Any) -> None:
        # A durable ref may have no local_path yet
        ref = FileReference(storage_path="artifacts/remote-only")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get",
            return_value={ref},
        ):
            result = await app.cleanup_files(
                CleanupInput(extra_paths=["nonexistent-dir"])
            )

        # No local_path → nothing deleted, no path_results entry for a ref
        assert "nonexistent-dir" in result.path_results

    @pytest.mark.asyncio
    async def test_removes_convention_based_temp_dir(self, tmp_path: Any) -> None:
        test_dir = tmp_path / "workflow-artifacts"
        test_dir.mkdir()
        (test_dir / "some-file.txt").write_text("data")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get", return_value=None
        ):
            with mock.patch(
                "application_sdk.constants.CLEANUP_BASE_PATHS", [str(test_dir)]
            ):
                result = await app.cleanup_files(CleanupInput())

        assert not test_dir.exists()
        assert result.path_results[str(test_dir)] is True

    @pytest.mark.asyncio
    async def test_nonexistent_path_treated_as_success(self, tmp_path: Any) -> None:
        missing = str(tmp_path / "nonexistent")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get", return_value=None
        ):
            with mock.patch("application_sdk.constants.CLEANUP_BASE_PATHS", [missing]):
                result = await app.cleanup_files(CleanupInput())

        assert result.path_results[missing] is True

    @pytest.mark.asyncio
    async def test_extra_paths_override_defaults(self, tmp_path: Any) -> None:
        extra_dir = tmp_path / "extra"
        extra_dir.mkdir()
        (extra_dir / "file.txt").write_text("x")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get", return_value=None
        ):
            result = await app.cleanup_files(CleanupInput(extra_paths=[str(extra_dir)]))

        assert not extra_dir.exists()
        assert result.path_results[str(extra_dir)] is True

    @pytest.mark.asyncio
    async def test_returns_cleanup_output(self, tmp_path: Any) -> None:
        app = _CleanupApp()

        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get", return_value=None
        ):
            result = await app.cleanup_files(
                CleanupInput(extra_paths=[str(tmp_path / "nonexistent")])
            )

        assert isinstance(result, CleanupOutput)
        assert isinstance(result.path_results, dict)

    @pytest.mark.asyncio
    async def test_error_during_file_removal_recorded_as_false(
        self, tmp_path: Any
    ) -> None:
        f = tmp_path / "locked.parquet"
        f.write_text("data")
        ref = FileReference(local_path=str(f), storage_path="artifacts/locked")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get",
            return_value={ref},
        ):
            with mock.patch("os.remove", side_effect=OSError("permission denied")):
                result = await app.cleanup_files(
                    CleanupInput(extra_paths=["nonexistent-dir"])
                )

        # Error is captured — task does not raise
        assert result.path_results[str(f)] is False
