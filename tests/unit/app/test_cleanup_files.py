"""Tests for App.cleanup_files() framework task."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, _app_state, _app_state_lock
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import get_task_metadata
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

    def test_heartbeat_timeout_seconds_is_explicit(self) -> None:
        # Regression guard for HB-12: cleanup_files must not silently inherit
        # ATLAN_HEARTBEAT_TIMEOUT_SECONDS (which individual apps set anywhere
        # from 60s to 7200s) — it needs its own small, explicit value that
        # stays well under its own timeout_seconds=300.
        metadata = get_task_metadata(App.cleanup_files)
        assert metadata is not None
        assert metadata.heartbeat_timeout_seconds == 60
        assert metadata.timeout_seconds == 300

    @pytest.mark.asyncio
    async def test_file_removal_offloaded_to_thread(self, tmp_path: Any) -> None:
        # Regression guard for HB-12: os.remove() must not run inline on the
        # event loop (that starves the auto-heartbeat for the call's duration).
        f = tmp_path / "output.parquet"
        f.write_text("data")
        ref = FileReference(local_path=str(f), storage_path="artifacts/x")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get",
            return_value={ref},
        ):
            with mock.patch(
                "application_sdk.execution.heartbeat.run_in_thread",
                new_callable=mock.AsyncMock,
                side_effect=lambda func, *a, **kw: func(*a, **kw),
            ) as mock_run_in_thread:
                result = await app.cleanup_files(
                    CleanupInput(extra_paths=["nonexistent-dir"])
                )

        assert not f.exists()
        assert result.path_results[str(f)] is True
        offloaded_funcs = [call.args[0] for call in mock_run_in_thread.call_args_list]
        assert os.remove in offloaded_funcs

    @pytest.mark.asyncio
    async def test_directory_removal_offloaded_to_thread(self, tmp_path: Any) -> None:
        # Same regression guard as above, for the shutil.rmtree() directory branch.
        test_dir = tmp_path / "workflow-artifacts"
        test_dir.mkdir()
        (test_dir / "some-file.txt").write_text("data")

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get", return_value=None
        ):
            with mock.patch(
                "application_sdk.execution.heartbeat.run_in_thread",
                new_callable=mock.AsyncMock,
                side_effect=lambda func, *a, **kw: func(*a, **kw),
            ) as mock_run_in_thread:
                result = await app.cleanup_files(
                    CleanupInput(extra_paths=[str(test_dir)])
                )

        assert not test_dir.exists()
        assert result.path_results[str(test_dir)] is True
        offloaded_funcs = [call.args[0] for call in mock_run_in_thread.call_args_list]
        assert shutil.rmtree in offloaded_funcs
