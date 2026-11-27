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
from application_sdk.execution._temporal import credential_sweep, run_scope
from application_sdk.execution._temporal.worker import AppWorker


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(credential_utils, "_DEFAULT_CREDENTIAL_FILE_DIR", str(tmp_path))
    with run_scope._credential_dirs_lock:
        run_scope._credential_dirs_by_run.clear()
    yield
    with run_scope._credential_dirs_lock:
        run_scope._credential_dirs_by_run.clear()


def _dir_of(run: tuple[str, str]) -> str:
    """A credential_dir() made by a task of *run* on this worker, with a secret in it."""
    with mock.patch.object(run_scope, "_current_run_key", return_value=run):
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
        assert run_scope._runs_with_credential_dirs() == [("wf-c", "run-live")]
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
        assert run_scope._runs_with_credential_dirs() == [("wf-a", "run-x")]

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
        assert run_scope._runs_with_credential_dirs() == []

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


# ---------------------------------------------------------------------------
# FND-5220: the run's _app_state slot is released on every worker, its
# values closed, once Temporal reports the run closed.
# ---------------------------------------------------------------------------


def _seed_slot(run: tuple[str, str], **values: Any) -> None:
    """A task of *run* on this worker stored *values* in its app-state slot."""
    with mock.patch.object(app_base, "_current_run_key", return_value=run):
        state = app_base.TaskStateAccessor()
        for key, value in values.items():
            state.set(key, value)


class _AsyncClient:
    """Stands in for an AsyncAtlanClient: closes with ``aclose()``."""

    def __init__(self) -> None:
        self.aclose = mock.AsyncMock()


class _SyncClient:
    """Stands in for a client with only a synchronous ``close()``."""

    def __init__(self) -> None:
        self.close = mock.Mock()


class TestReleaseAppState:
    async def test_closed_runs_slots_are_released_and_running_ones_kept(
        self,
    ) -> None:
        async_client, sync_client, live_client = (
            _AsyncClient(),
            _SyncClient(),
            _AsyncClient(),
        )
        _seed_slot(("wf-a", "run-done"), atlan=async_client, source=sync_client)
        _seed_slot(("wf-b", "run-live"), atlan=live_client)

        await credential_sweep.sweep_closed_runs(
            _client(
                {
                    "run-done": WorkflowExecutionStatus.COMPLETED,
                    "run-live": WorkflowExecutionStatus.RUNNING,
                }
            )
        )

        assert app_base._runs_with_app_state() == [("wf-b", "run-live")]
        async_client.aclose.assert_awaited_once()
        sync_client.close.assert_called_once()
        live_client.aclose.assert_not_awaited()

    async def test_a_value_cached_under_two_keys_is_closed_once(self) -> None:
        client = _AsyncClient()
        _seed_slot(("wf-a", "run-done"), validated=client, named=client)

        await app_base._release_run_state(("wf-a", "run-done"))

        client.aclose.assert_awaited_once()

    async def test_one_failing_close_does_not_stop_the_rest(self) -> None:
        broken, ok = _AsyncClient(), _AsyncClient()
        broken.aclose.side_effect = RuntimeError("socket gone")
        _seed_slot(("wf-a", "run-done"), broken=broken, ok=ok)

        await app_base._release_run_state(("wf-a", "run-done"))

        ok.aclose.assert_awaited_once()
        assert app_base._runs_with_app_state() == []

    async def test_values_without_a_close_and_classes_are_left_alone(self) -> None:
        _seed_slot(("wf-a", "run-done"), number=3, cls=_SyncClient, none=None)

        await app_base._release_run_state(("wf-a", "run-done"))

        assert app_base._runs_with_app_state() == []

    async def test_tracked_local_files_go_with_the_slot(self, tmp_path: Any) -> None:
        from application_sdk.constants import TRACKED_FILE_REFS_KEY
        from application_sdk.contracts.types import FileReference

        data = tmp_path / "part-0.parquet"
        data.write_bytes(b"rows")
        sidecar = tmp_path / "part-0.parquet.sha256"
        sidecar.write_text("abc")
        _seed_slot(
            ("wf-a", "run-done"),
            **{TRACKED_FILE_REFS_KEY: {FileReference(local_path=str(data))}},
        )

        await app_base._release_run_state(("wf-a", "run-done"))

        assert not data.exists() and not sidecar.exists()

    async def test_tracked_local_files_stay_when_cleanup_is_off(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.constants import TRACKED_FILE_REFS_KEY
        from application_sdk.contracts.types import FileReference

        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")
        data = tmp_path / "part-0.parquet"
        data.write_bytes(b"rows")
        _seed_slot(
            ("wf-a", "run-done"),
            **{TRACKED_FILE_REFS_KEY: {FileReference(local_path=str(data))}},
        )

        await app_base._release_run_state(("wf-a", "run-done"))

        assert data.exists()
        assert app_base._runs_with_app_state() == []

    async def test_two_workers_each_release_their_own_slot(self) -> None:
        """Extract ran on worker A, cleanup on worker B: both end empty.

        Each worker's sweeper sees only its own process, so the two are
        simulated one after the other against the same fake Temporal.
        """
        temporal = _client({"run-1": WorkflowExecutionStatus.COMPLETED})
        for client_on_worker in (_AsyncClient(), _AsyncClient()):
            _seed_slot(("wf-a", "run-1"), atlan=client_on_worker)
            await credential_sweep.sweep_closed_runs(temporal)
            assert app_base._runs_with_app_state() == []
            client_on_worker.aclose.assert_awaited_once()

    async def test_worker_shutdown_releases_every_slot(self) -> None:
        client = _AsyncClient()
        _seed_slot(("wf-a", "run-live"), atlan=client)
        inner = mock.AsyncMock()
        app_worker = AppWorker(
            inner, start_event_params={}, shutdown_drain_delay_seconds=0
        )

        with mock.patch(
            "application_sdk.execution._temporal.worker._emit_worker_start_event"
        ):
            async with app_worker:
                assert app_base._runs_with_app_state() == [("wf-a", "run-live")]

        assert app_base._runs_with_app_state() == []
        client.aclose.assert_awaited_once()


class TestReleaseIsBoundedAndCancellationSafe:
    async def test_a_hung_sync_close_is_abandoned_off_the_shared_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import threading

        release = threading.Event()

        class _Hangs:
            def __init__(self) -> None:
                self.close = mock.Mock(side_effect=lambda: release.wait(5))

        monkeypatch.setattr(app_base, "_STATE_VALUE_CLOSE_TIMEOUT_SECONDS", 0.05)
        pool = mock.AsyncMock()
        monkeypatch.setattr(app_base, "run_in_thread", pool)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await app_base._close_state_value(_Hangs())
            assert any(t.name == "app-state-close" for t in threading.enumerate())
            pool.assert_not_awaited()  # the shared blocking pool is never used
        finally:
            release.set()

    async def test_shutdown_finishes_a_release_the_cancelled_sweeper_started(
        self,
    ) -> None:
        import asyncio

        started, finish = asyncio.Event(), asyncio.Event()
        client = _AsyncClient()

        async def _slow_close() -> None:
            started.set()
            await finish.wait()

        client.aclose.side_effect = _slow_close
        _seed_slot(("wf-a", "run-done"), atlan=client)

        sweep = asyncio.create_task(app_base._release_run_state(("wf-a", "run-done")))
        await started.wait()
        sweep.cancel()  # what stopping the sweeper does at shutdown
        with pytest.raises(asyncio.CancelledError):
            await sweep
        assert app_base._runs_with_app_state() == []  # slot already taken out

        finish.set()
        await credential_sweep.release_all_runs()

        client.aclose.assert_awaited_once()
        assert not app_base._releases_in_flight
