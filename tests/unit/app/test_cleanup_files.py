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
from application_sdk.constants import TRACKED_CREDENTIAL_DIRS_KEY, TRACKED_FILE_REFS_KEY
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import CleanupInput, CleanupOutput
from application_sdk.contracts.types import FileReference
from tests.unit.private_stdlib import private_stdlib


@dataclass
class _CFInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _CFOutput(Output, allow_unbounded_fields=True):
    result: str = ""


def _tracked_refs_only(refs: Any) -> Any:
    """A ``TaskStateAccessor.get`` stand-in that holds only tracked file refs."""

    def _get(key: str) -> Any:
        return refs if key == TRACKED_FILE_REFS_KEY else None

    return _get


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
            side_effect=_tracked_refs_only({ref}),
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
            side_effect=_tracked_refs_only({ref}),
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
    async def test_default_sweep_removes_path_less_materialised_refs(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FND-4282: refs with only a ``storage_path`` go with the run.

        Both shapes materialise under the run's scratch directory, and the
        default sweep — no ``extra_paths``, no ``CLEANUP_BASE_PATHS`` — removes
        them without either being tracked.
        """
        from types import SimpleNamespace

        from temporalio import activity

        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _put
        from application_sdk.storage.reference import materialize_file_reference
        from application_sdk.testing.fixtures import _redirect_temporary_path

        sdk_tmp = tmp_path / "sdk-tmp"
        _redirect_temporary_path(monkeypatch, str(sdk_tmp))
        monkeypatch.setattr(
            activity,
            "info",
            lambda: SimpleNamespace(workflow_id="wf-1", workflow_run_id="run-1"),
        )
        store = create_memory_store()
        await _put("k/one.bin", b"one", store, normalize=False)
        await _put("d/a.txt", b"a", store, normalize=False)
        file_ref = await materialize_file_reference(
            store, FileReference(is_durable=True, storage_path="k/one.bin")
        )
        dir_ref = await materialize_file_reference(
            store, FileReference(is_durable=True, storage_path="d/")
        )
        assert file_ref.local_path is not None and dir_ref.local_path is not None
        for local in (file_ref.local_path, dir_ref.local_path):
            assert os.path.realpath(local).startswith(os.path.realpath(sdk_tmp))
            assert os.path.exists(local)

        app = _CleanupApp()
        with (
            mock.patch(
                "application_sdk.app.base.TaskStateAccessor.get", return_value=None
            ),
            mock.patch("application_sdk.constants.CLEANUP_BASE_PATHS", []),
        ):
            result = await app.cleanup_files(CleanupInput())

        assert not os.path.exists(file_ref.local_path)
        assert not os.path.exists(dir_ref.local_path)
        assert all(result.path_results.values())

    @pytest.mark.asyncio
    async def test_a_climbing_workflow_id_deletes_nothing_outside_temporary_path(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default sweep must not ``rmtree`` what a ``..`` workflow ID points at."""
        from types import SimpleNamespace

        from temporalio import activity

        from application_sdk.testing.fixtures import _redirect_temporary_path

        sdk_tmp = tmp_path / "sdk-tmp"
        sdk_tmp.mkdir()
        # artifacts/apps/{app}/workflows/{wf}/{run}: five segments below the
        # root, so six ``..`` climb one above it into tmp_path.
        victim = tmp_path / "victim" / "run-1"
        victim.mkdir(parents=True)
        (victim / "keep.txt").write_text("data")
        _redirect_temporary_path(monkeypatch, str(sdk_tmp))
        monkeypatch.setattr(
            activity,
            "info",
            lambda: SimpleNamespace(
                workflow_id="../../../../../victim", workflow_run_id="run-1"
            ),
        )

        app = _CleanupApp()
        with (
            mock.patch(
                "application_sdk.app.base.TaskStateAccessor.get", return_value=None
            ),
            mock.patch("application_sdk.constants.CLEANUP_BASE_PATHS", []),
        ):
            result = await app.cleanup_files(CleanupInput())

        assert (victim / "keep.txt").exists()
        assert result.path_results == {}

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
            side_effect=_tracked_refs_only({ref}),
        ):
            with (
                private_stdlib("application_sdk.app.base", "os") as local_os,
                mock.patch.object(
                    local_os, "remove", side_effect=OSError("permission denied")
                ),
            ):
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
            side_effect=_tracked_refs_only(tracked_refs),
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


class TestCleanupCredentialFiles:
    """FND-4275: credential files resolved during a run go with the run.

    ``resolve_credential_file`` gives every call its own 0700 directory and
    registers it with the run; ``cleanup_files`` removes those directories
    whatever ``extra_paths`` says, so a keytab or private key does not
    outlive the run on a long-lived worker.
    """

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
    async def test_same_named_files_for_two_connections_are_private_and_cleaned_up(
        self, tmp_path: Any
    ) -> None:
        import base64

        from application_sdk.credentials.utils import resolve_credential_file

        keytab_a, keytab_b = b"connection-a-keytab", b"connection-b-keytab"
        with (
            mock.patch("temporalio.activity.in_activity", return_value=True),
            mock.patch.object(
                app_base, "_get_execution_id_from_task", return_value="wf-1"
            ),
        ):
            path_a = await resolve_credential_file(
                base64.b64encode(keytab_a).decode(),
                "keytab.keytab",
                str(tmp_path),
                raise_on_error=True,
            )
            path_b = await resolve_credential_file(
                base64.b64encode(keytab_b).decode(),
                "keytab.keytab",
                str(tmp_path),
                raise_on_error=True,
            )

            assert path_a != path_b
            with open(path_a, "rb") as f:
                assert f.read() == keytab_a
            with open(path_b, "rb") as f:
                assert f.read() == keytab_b
            if os.name == "posix":
                for path in (path_a, path_b):
                    assert os.stat(path).st_mode & 0o777 == 0o600
                    assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700

            # extra_paths would skip every convention-based sweep; the
            # credential directories are removed regardless.
            result = await _CleanupApp().cleanup_files(
                CleanupInput(extra_paths=[str(tmp_path / "nonexistent-dir")])
            )

        for path in (path_a, path_b):
            call_dir = os.path.dirname(path)
            assert not os.path.exists(call_dir)
            assert result.path_results[call_dir] is True
        with _app_state_lock:
            assert not _app_state["wf-1"][TRACKED_CREDENTIAL_DIRS_KEY]

    @pytest.mark.asyncio
    async def test_failed_removal_stays_tracked(self, tmp_path: Any) -> None:
        cred_dir = tmp_path / "cred-x"
        cred_dir.mkdir()
        (cred_dir / "keytab.keytab").write_bytes(b"secret")
        with _app_state_lock:
            _app_state["wf-1"] = {TRACKED_CREDENTIAL_DIRS_KEY: {str(cred_dir)}}

        with (
            mock.patch.object(
                app_base, "_get_execution_id_from_task", return_value="wf-1"
            ),
            private_stdlib(app_base, "shutil") as local_shutil,
        ):
            local_shutil.rmtree = mock.Mock(side_effect=PermissionError("denied"))
            result = await _CleanupApp().cleanup_files(
                CleanupInput(extra_paths=[str(tmp_path / "nonexistent-dir")])
            )

        assert result.path_results[str(cred_dir)] is False
        with _app_state_lock:
            assert _app_state["wf-1"][TRACKED_CREDENTIAL_DIRS_KEY] == {str(cred_dir)}

    def test_tracking_outside_a_task_raises(self) -> None:
        from application_sdk._context_errors import AppContextError
        from application_sdk.app.base import _track_credential_dir

        with pytest.raises(AppContextError):
            _track_credential_dir("/somewhere")
