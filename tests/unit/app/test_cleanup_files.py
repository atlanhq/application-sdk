"""Tests for App.cleanup_files() framework task."""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest

from application_sdk.app import base as app_base
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
    @pytest.mark.parametrize("section", ["tracked_ref", "extra_paths"])
    @pytest.mark.parametrize("kind", ["file", "dir"])
    async def test_removal_runs_off_the_event_loop(
        self, tmp_path: Any, section: str, kind: str
    ) -> None:
        # Regression guard for HB-12: every removal cleanup_files performs must
        # run off the event loop, or it starves the auto-heartbeat for the
        # call's duration. Parametrised over both sections (tracked
        # FileReference paths / extra_paths) and both branches (file / dir) so
        # all four call sites are covered — re-inlining any one of them fails.
        #
        # This drives the real run_in_thread (real executor, real worker
        # thread) and records where the fs call actually ran, so the assertion
        # tracks the property the fix is about rather than merely which
        # callable was handed to the wrapper. Patched on ``app.base``, the
        # consuming module: since ADR-0019 it binds ``run_in_thread`` at module
        # scope, so patching the substrate module would miss this reference.
        if kind == "dir":
            target = tmp_path / "workflow-artifacts"
            target.mkdir()
            (target / "some-file.txt").write_text("data")
            expected_func: Any = shutil.rmtree
        else:
            target = tmp_path / "output.parquet"
            target.write_text("data")
            expected_func = os.remove

        real_run_in_thread = app_base.run_in_thread
        calls: list[tuple[Any, int]] = []

        async def recording_run_in_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            def wrapped(*a: Any, **kw: Any) -> Any:
                calls.append((func, threading.get_ident()))
                return func(*a, **kw)

            return await real_run_in_thread(wrapped, *args, **kwargs)

        tracked_refs = None
        extra_paths = ["nonexistent-dir"]
        if section == "tracked_ref":
            tracked_refs = {
                FileReference(local_path=str(target), storage_path="artifacts/x")
            }
        else:
            extra_paths = [str(target)]

        app = _CleanupApp()
        with mock.patch(
            "application_sdk.app.base.TaskStateAccessor.get",
            return_value=tracked_refs,
        ):
            with mock.patch.object(app_base, "run_in_thread", recording_run_in_thread):
                result = await app.cleanup_files(CleanupInput(extra_paths=extra_paths))

        assert not target.exists()
        assert result.path_results[str(target)] is True
        # Exactly one offload, using the branch-appropriate callable …
        assert [func for func, _ in calls] == [expected_func]
        # … and it did not execute on the event loop's thread.
        loop_thread = threading.get_ident()
        assert all(ident != loop_thread for _, ident in calls)
