"""FND-4275: a worker removes its own credential directories once their run closes.

``cleanup_files`` can run on a different worker from the task that wrote a
run's credential directories, so each worker sweeps what it wrote.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from application_sdk.app import base as app_base
from application_sdk.credentials import utils as credential_utils
from application_sdk.execution._temporal import credential_sweep
from application_sdk.execution._temporal.worker import AppWorker


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(credential_utils, "_DEFAULT_CREDENTIAL_FILE_DIR", str(tmp_path))
    with app_base._credential_dirs_lock:
        app_base._credential_dirs_by_run.clear()
    yield
    with app_base._credential_dirs_lock:
        app_base._credential_dirs_by_run.clear()


def _dir_of(run: tuple[str, str]) -> str:
    """A credential_dir() made by a task of *run* on this worker, with a secret in it."""
    with mock.patch.object(app_base, "_current_run_key", return_value=run):
        d = credential_utils.credential_dir()
    with open(os.path.join(d, "keytab"), "wb") as f:
        f.write(b"secret")
    return d


def _client(statuses: dict[str, WorkflowExecutionStatus | Exception]) -> mock.Mock:
    """A Temporal client whose describe() answers per run_id."""

    def get_workflow_handle(workflow_id: str, *, run_id: str) -> mock.Mock:
        answer = statuses[run_id]
        handle = mock.Mock()
        if isinstance(answer, Exception):
            handle.describe = mock.AsyncMock(side_effect=answer)
        else:
            handle.describe = mock.AsyncMock(return_value=mock.Mock(status=answer))
        return handle

    client = mock.Mock()
    client.get_workflow_handle = mock.Mock(side_effect=get_workflow_handle)
    return client


class TestSweepClosedRuns:
    async def test_closed_runs_are_removed_and_running_ones_kept(self) -> None:
        done = _dir_of(("wf-a", "run-done"))
        failed = _dir_of(("wf-b", "run-failed"))
        live = _dir_of(("wf-c", "run-live"))

        await credential_sweep.sweep_closed_runs(
            _client(
                {
                    "run-done": WorkflowExecutionStatus.COMPLETED,
                    "run-failed": WorkflowExecutionStatus.FAILED,
                    "run-live": WorkflowExecutionStatus.RUNNING,
                }
            )
        )

        assert not os.path.lexists(done) and not os.path.lexists(failed)
        assert os.path.isdir(live)
        assert app_base._runs_with_credential_dirs() == [("wf-c", "run-live")]
        assert done not in credential_utils._issued_dirs
        assert live in credential_utils._issued_dirs

    async def test_a_run_temporal_no_longer_knows_is_removed(self) -> None:
        gone = _dir_of(("wf-a", "run-gone"))
        await credential_sweep.sweep_closed_runs(
            _client({"run-gone": RPCError("not found", RPCStatusCode.NOT_FOUND, b"")})
        )
        assert not os.path.lexists(gone)

    async def test_an_unanswered_status_keeps_the_dir_for_the_next_sweep(self) -> None:
        kept = _dir_of(("wf-a", "run-x"))
        await credential_sweep.sweep_closed_runs(
            _client({"run-x": RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")})
        )
        assert os.path.isdir(kept)
        assert app_base._runs_with_credential_dirs() == [("wf-a", "run-x")]

    async def test_nothing_tracked_means_no_temporal_calls(self) -> None:
        client = _client({})
        await credential_sweep.sweep_closed_runs(client)
        client.get_workflow_handle.assert_not_called()


class TestWorkerLifecycle:
    async def test_the_worker_runs_the_sweeper_and_removes_everything_at_shutdown(
        self,
    ) -> None:
        live = _dir_of(("wf-a", "run-live"))
        inner = mock.AsyncMock()
        app_worker = AppWorker(
            inner, start_event_params={}, shutdown_drain_delay_seconds=0
        )

        with mock.patch(
            "application_sdk.execution._temporal.worker._emit_worker_start_event"
        ):
            async with app_worker:
                sweeper = app_worker._credential_sweeper
                assert sweeper is not None and not sweeper.done()
                assert os.path.isdir(live)

        assert sweeper.done()
        assert app_worker._credential_sweeper is None
        assert not os.path.lexists(live)
        assert app_base._runs_with_credential_dirs() == []

    async def test_run_removes_everything_when_the_worker_stops(self) -> None:
        live = _dir_of(("wf-a", "run-live"))
        inner = mock.AsyncMock()
        inner.run = mock.AsyncMock(return_value=None)
        app_worker = AppWorker(inner, start_event_params={})

        with mock.patch(
            "application_sdk.execution._temporal.worker._emit_worker_start_event"
        ):
            await app_worker.run()

        assert not os.path.lexists(live)
