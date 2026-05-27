"""Tests for the deterministic cross-worker local FileReference GC sweep."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from application_sdk.storage.local_gc import (
    SweepResult,
    _describe_status,
    run_scoped_dir,
    sweep_local_file_refs,
)

# --- WorkflowExecutionStatus partitioning -----------------------------------

TERMINAL_STATUSES = [
    WorkflowExecutionStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
    WorkflowExecutionStatus.CONTINUED_AS_NEW,
]


# --- Fake Temporal client ----------------------------------------------------


class _FakeHandle:
    def __init__(self, behavior: tuple[Any, ...]) -> None:
        self._behavior = behavior

    async def describe(self) -> Any:
        kind = self._behavior[0]
        if kind == "status":
            return type("Desc", (), {"status": self._behavior[1]})()
        if kind == "not_found":
            raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
        if kind == "raise":
            raise self._behavior[1]
        raise AssertionError(f"unknown behavior {kind!r}")


class _FakeClient:
    """Returns a handle whose ``describe()`` is keyed by ``run_id``.

    ``behaviors`` maps run_id -> ("status", WorkflowExecutionStatus)
    | ("not_found",) | ("raise", Exception).
    """

    def __init__(self, behaviors: dict[str, tuple[Any, ...]]) -> None:
        self._behaviors = behaviors
        self.handle_calls: list[tuple[str, str | None]] = []

    def get_workflow_handle(
        self, workflow_id: str, *, run_id: str | None = None
    ) -> _FakeHandle:
        self.handle_calls.append((workflow_id, run_id))
        return _FakeHandle(self._behaviors[run_id])


# --- Helpers -----------------------------------------------------------------


def _make_run_dir(root: Path, workflow_id: str, run_id: str) -> Path:
    """Create ``{root}/{workflow_id}/{run_id}/`` with a scratch file inside."""
    run_dir = root / workflow_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "io_pairs.json").write_text("[]")
    return run_dir


# --- sweep_local_file_refs ---------------------------------------------------


class TestSweep:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", TERMINAL_STATUSES)
    async def test_terminal_status_dir_is_deleted(
        self, tmp_path: Path, status: WorkflowExecutionStatus
    ) -> None:
        run_dir = _make_run_dir(tmp_path, "wf", "run-1")
        client = _FakeClient({"run-1": ("status", status)})

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=50, root=str(tmp_path)
        )

        assert not run_dir.exists()
        assert str(run_dir) in result.deleted
        assert str(run_dir) not in result.kept
        assert result.describes == 1

    @pytest.mark.asyncio
    async def test_running_status_dir_is_kept(self, tmp_path: Path) -> None:
        run_dir = _make_run_dir(tmp_path, "wf", "run-1")
        client = _FakeClient({"run-1": ("status", WorkflowExecutionStatus.RUNNING)})

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=50, root=str(tmp_path)
        )

        assert run_dir.exists()
        assert str(run_dir) in result.kept
        assert str(run_dir) not in result.deleted

    @pytest.mark.asyncio
    async def test_not_found_dir_is_deleted(self, tmp_path: Path) -> None:
        run_dir = _make_run_dir(tmp_path, "wf", "run-1")
        client = _FakeClient({"run-1": ("not_found",)})

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=50, root=str(tmp_path)
        )

        assert not run_dir.exists()
        assert str(run_dir) in result.deleted

    @pytest.mark.asyncio
    async def test_transient_error_dir_is_kept(self, tmp_path: Path) -> None:
        run_dir = _make_run_dir(tmp_path, "wf", "run-1")
        transient = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        client = _FakeClient({"run-1": ("raise", transient)})

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=50, root=str(tmp_path)
        )

        assert run_dir.exists()
        assert str(run_dir) in result.kept
        assert str(run_dir) not in result.deleted

    @pytest.mark.asyncio
    async def test_current_run_dir_is_skipped_and_never_described(
        self, tmp_path: Path
    ) -> None:
        current = _make_run_dir(tmp_path, "wf", "current-run")
        # Even if Temporal would report it COMPLETED, the current run is never touched.
        client = _FakeClient(
            {"current-run": ("status", WorkflowExecutionStatus.COMPLETED)}
        )

        result = await sweep_local_file_refs(
            client,
            current_run_id="current-run",
            max_describes=50,
            root=str(tmp_path),
        )

        assert current.exists()
        assert result.describes == 0
        assert client.handle_calls == []  # never even asked Temporal
        assert str(current) not in result.deleted

    @pytest.mark.asyncio
    async def test_max_describes_cap_honored(self, tmp_path: Path) -> None:
        run_dirs = [_make_run_dir(tmp_path, "wf", f"run-{i}") for i in range(5)]
        client = _FakeClient(
            {
                f"run-{i}": ("status", WorkflowExecutionStatus.COMPLETED)
                for i in range(5)
            }
        )

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=2, root=str(tmp_path)
        )

        # Only two describes issued this sweep; the rest are deferred.
        assert result.describes == 2
        assert len(client.handle_calls) == 2
        assert len(result.deleted) == 2
        surviving = [d for d in run_dirs if d.exists()]
        assert len(surviving) == 3

    @pytest.mark.asyncio
    async def test_delete_failure_keeps_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = _make_run_dir(tmp_path, "wf", "run-1")
        client = _FakeClient({"run-1": ("status", WorkflowExecutionStatus.COMPLETED)})

        def _boom(_path: Any) -> None:
            raise OSError("device busy")

        monkeypatch.setattr("application_sdk.storage.local_gc.shutil.rmtree", _boom)

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=50, root=str(tmp_path)
        )

        # Undeletable dir is kept (retried next sweep), never reported as deleted.
        assert run_dir.exists()
        assert str(run_dir) in result.kept
        assert str(run_dir) not in result.deleted

    @pytest.mark.asyncio
    async def test_missing_root_returns_empty_result(self, tmp_path: Path) -> None:
        client = _FakeClient({})

        result = await sweep_local_file_refs(
            client,
            current_run_id="other",
            max_describes=50,
            root=str(tmp_path / "does-not-exist"),
        )

        assert result == SweepResult()
        assert client.handle_calls == []

    @pytest.mark.asyncio
    async def test_mixed_runs_partition_correctly(self, tmp_path: Path) -> None:
        done = _make_run_dir(tmp_path, "wf-a", "done")
        live = _make_run_dir(tmp_path, "wf-b", "live")
        gone = _make_run_dir(tmp_path, "wf-c", "gone")
        client = _FakeClient(
            {
                "done": ("status", WorkflowExecutionStatus.COMPLETED),
                "live": ("status", WorkflowExecutionStatus.RUNNING),
                "gone": ("not_found",),
            }
        )

        result = await sweep_local_file_refs(
            client, current_run_id="other", max_describes=50, root=str(tmp_path)
        )

        assert not done.exists()
        assert live.exists()
        assert not gone.exists()
        assert sorted(result.deleted) == sorted([str(done), str(gone)])
        assert result.kept == [str(live)]


# --- _describe_status --------------------------------------------------------


class TestDescribeStatus:
    @pytest.mark.asyncio
    async def test_returns_status_for_known_run(self) -> None:
        client = _FakeClient({"r": ("status", WorkflowExecutionStatus.FAILED)})
        assert (
            await _describe_status(client, "wf", "r") == WorkflowExecutionStatus.FAILED
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_not_found(self) -> None:
        client = _FakeClient({"r": ("not_found",)})
        assert await _describe_status(client, "wf", "r") is None

    @pytest.mark.asyncio
    async def test_reraises_non_not_found_rpc_error(self) -> None:
        transient = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        client = _FakeClient({"r": ("raise", transient)})
        with pytest.raises(RPCError):
            await _describe_status(client, "wf", "r")


# --- run_scoped_dir ----------------------------------------------------------


class TestRunScopedDir:
    """The single source of the GC path scheme: {root}/{sanitize(wf)}/{run}/.

    local_run_dir(), the activity auto-materialize dir, and the sweep walk must
    all agree on this layout, so it lives in one place.
    """

    def test_builds_run_scoped_path(self, tmp_path: Path) -> None:
        path = run_scoped_dir("wf1", "run1", root=str(tmp_path))
        assert path == tmp_path / "wf1" / "run1"

    def test_sanitizes_slashes_in_workflow_id(self, tmp_path: Path) -> None:
        path = run_scoped_dir("ns/team/wf-7", "run-1", root=str(tmp_path))
        assert path == tmp_path / "ns_team_wf-7" / "run-1"

    def test_does_not_create_by_default(self, tmp_path: Path) -> None:
        path = run_scoped_dir("wf1", "run1", root=str(tmp_path))
        assert not path.exists()

    def test_create_true_makes_the_directory(self, tmp_path: Path) -> None:
        path = run_scoped_dir("wf1", "run1", root=str(tmp_path), create=True)
        assert path.is_dir()

    def test_defaults_root_to_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.LOCAL_FILE_REF_ROOT", str(tmp_path)
        )
        path = run_scoped_dir("wf1", "run1")
        assert path == tmp_path / "wf1" / "run1"
