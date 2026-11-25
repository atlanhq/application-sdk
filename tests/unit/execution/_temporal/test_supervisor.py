"""Unit tests for the worker restart supervisor and poll-state observer
(``application_sdk.execution._temporal.supervisor``).

Moved from ``tests/unit/test_main.py`` with the code (FND-4689)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.execution._temporal.supervisor import (
    _FatalTeardownWatchdog,
    _observe_worker_poll_state,
    _run_worker_with_restart,
)
from tests.unit.private_stdlib import private_stdlib


class _FakeWorker:
    """Minimal async-context-manager stand-in for an AppWorker.

    ``fail=True`` makes ``__aenter__`` raise the same shape of error temporalio
    surfaces on a fatal poll. ``on_enter`` runs on enter (e.g. to set the
    shutdown event so the supervisor's ``await shutdown_event.wait()`` returns).
    """

    def __init__(self, *, fail: bool, on_enter: Any = None) -> None:
        self.fail = fail
        self.on_enter = on_enter

    async def __aenter__(self) -> _FakeWorker:
        if self.on_enter is not None:
            self.on_enter()
        if self.fail:
            raise RuntimeError("Activity worker failed")
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _CancelThenFailWorker:
    """Models temporalio's *real* fatal-poll seam.

    Unlike ``_FakeWorker(fail=True)`` (which raises from ``__aenter__``),
    temporalio surfaces a fatal poll by cancelling the ``async with`` body task
    mid-``await`` and re-raising the fatal error from ``__aexit__`` only when
    ``exc_type is CancelledError``. That leaves a residual cancellation on the
    task, which the supervisor must clear with ``uncancel()`` before its backoff
    ``wait_for`` — the exact line ``_FakeWorker`` never exercises.
    """

    def __init__(self, *, on_exit: Any = None) -> None:
        self.on_exit = on_exit

    async def __aenter__(self) -> _CancelThenFailWorker:
        # Cancel the task running the `async with` body so the following
        # `await shutdown_event.wait()` is torn down mid-await.
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return self

    async def __aexit__(self, exc_type: Any, *args: Any) -> bool:
        if self.on_exit is not None:
            self.on_exit()
        if exc_type is asyncio.CancelledError:
            # temporalio re-raises the fatal error from __aexit__ on the seam.
            raise RuntimeError("Activity worker failed")
        return False


class TestRunWorkerWithRestart:
    """Behavioral tests for the worker restart supervisor."""

    async def test_clean_shutdown_runs_once_no_restart(self) -> None:
        """A shutdown signal stops the loop without rebuilding the worker."""
        shutdown = asyncio.Event()
        built: list[_FakeWorker] = []

        def build() -> _FakeWorker:
            w = _FakeWorker(fail=False, on_enter=shutdown.set)
            built.append(w)
            return w

        await _run_worker_with_restart(build_worker=build, shutdown_event=shutdown)

        assert len(built) == 1

    async def test_restarts_after_fatal_then_shuts_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fatal error rebuilds the worker; a fresh token is minted first."""
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        shutdown = asyncio.Event()
        auth = AsyncMock()
        calls = {"n": 0}

        def build() -> _FakeWorker:
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeWorker(fail=True)
            return _FakeWorker(fail=False, on_enter=shutdown.set)

        await _run_worker_with_restart(
            build_worker=build,
            shutdown_event=shutdown,
            auth_manager=auth,
            client=object(),
        )

        assert calls["n"] == 2
        auth.force_refresh.assert_awaited_once()

    async def test_reconnect_runs_before_rebuild_and_replaces_force_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wired reconnect rebuilds the client before the worker is rebuilt.

        Ordering is the whole point: the replacement worker has to be built
        against the fresh client. Built against the poisoned one it comes up,
        registers, and then never polls — and because it neither returns nor
        raises, the supervisor never gets a second chance to notice.
        """
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        shutdown = asyncio.Event()
        auth = AsyncMock()
        order: list[str] = []
        calls = {"n": 0}

        async def reconnect() -> None:
            order.append("reconnect")

        def build() -> _FakeWorker:
            calls["n"] += 1
            order.append(f"build{calls['n']}")
            if calls["n"] == 1:
                return _FakeWorker(fail=True)
            return _FakeWorker(fail=False, on_enter=shutdown.set)

        await _run_worker_with_restart(
            build_worker=build,
            shutdown_event=shutdown,
            auth_manager=auth,
            client=object(),
            reconnect=reconnect,
        )

        assert order == ["build1", "reconnect", "build2"]
        # Reconnect mints its own token, so the token-only path is skipped.
        auth.force_refresh.assert_not_awaited()

    async def test_restart_proceeds_when_reconnect_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing reconnect must not strand the worker.

        If Temporal is unreachable at that moment, restarting on the old client
        is still better than not restarting at all — the next fatal brings us
        back here after another backoff.
        """
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        shutdown = asyncio.Event()
        calls = {"n": 0}

        async def reconnect() -> None:
            raise RuntimeError("temporal unreachable")

        def build() -> _FakeWorker:
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeWorker(fail=True)
            return _FakeWorker(fail=False, on_enter=shutdown.set)

        await _run_worker_with_restart(
            build_worker=build,
            shutdown_event=shutdown,
            reconnect=reconnect,
        )

        assert calls["n"] == 2

    async def test_restarts_on_cancellation_seam_without_propagating_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fatal error arriving via the temporalio cancellation seam self-heals.

        The first worker cancels the body task and re-raises the fatal error
        from ``__aexit__`` (exercising the supervisor's ``uncancel()`` guard);
        the supervisor must clear the residual cancel, restart, and reach a
        clean shutdown without ever letting ``CancelledError`` escape.
        """
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        shutdown = asyncio.Event()
        calls = {"n": 0}

        def build() -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return _CancelThenFailWorker()
            return _FakeWorker(fail=False, on_enter=shutdown.set)

        # Must complete normally: no CancelledError propagates out, and the
        # supervisor rebuilt the worker after the seam failure.
        await _run_worker_with_restart(build_worker=build, shutdown_event=shutdown)

        assert calls["n"] == 2

    async def test_gives_up_after_cap_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistent failure fails loud once the restart cap is exceeded."""
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_MAX_CONSECUTIVE_RESTARTS",
            2,
        )
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        shutdown = asyncio.Event()
        calls = {"n": 0}

        def build() -> _FakeWorker:
            calls["n"] += 1
            return _FakeWorker(fail=True)

        with pytest.raises(RuntimeError, match="Activity worker failed"):
            await _run_worker_with_restart(build_worker=build, shutdown_event=shutdown)

        # attempts 1 and 2 restart; the 3rd pushes the streak past the cap.
        assert calls["n"] == 3

    async def test_shutdown_during_backoff_stops_promptly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shutdown set during the backoff wait ends the loop immediately."""
        # Force a long backoff so the shutdown interrupts it deterministically.
        # The delay is drawn immediately before the backoff wait, with no await
        # in between, so once it is drawn the supervisor is about to wait on it.
        backing_off = asyncio.Event()

        def long_backoff(*_a: Any) -> float:
            backing_off.set()
            return 10.0

        monkeypatch.setattr(
            "application_sdk.common.backoff.random.uniform", long_backoff
        )
        shutdown = asyncio.Event()
        calls = {"n": 0}

        def build() -> _FakeWorker:
            calls["n"] += 1
            return _FakeWorker(fail=True)

        task = asyncio.create_task(
            _run_worker_with_restart(build_worker=build, shutdown_event=shutdown)
        )
        await asyncio.wait_for(backing_off.wait(), timeout=5)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

        assert calls["n"] == 1

    async def test_healthy_run_resets_failure_streak(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that ran healthily before failing resets the streak.

        With a cap of 2 but each run appearing to last past the healthy window,
        the streak resets every time, so four failures never trip the cap and
        the loop survives to a clean shutdown.
        """
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_MAX_CONSECUTIVE_RESTARTS",
            2,
        )
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_HEALTHY_RUN_SECONDS",
            1,
        )
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        # Clock jumps 5s on every read → each run looks like it lasted 5s (>1s).
        counter = {"t": 0.0}

        def fake_monotonic() -> float:
            counter["t"] += 5.0
            return counter["t"]

        shutdown = asyncio.Event()
        calls = {"n": 0}

        def build() -> _FakeWorker:
            calls["n"] += 1
            if calls["n"] <= 4:
                return _FakeWorker(fail=True)
            return _FakeWorker(fail=False, on_enter=shutdown.set)

        with private_stdlib(
            "application_sdk.execution._temporal.supervisor", "time"
        ) as local_time:
            monkeypatch.setattr(local_time, "monotonic", fake_monotonic)
            # Must not raise despite 4 failures (cap is 2) because each resets.
            await _run_worker_with_restart(build_worker=build, shutdown_event=shutdown)

        assert calls["n"] == 5


class _RecordingHealthServer:
    """Records the poll-state readings the observer reports."""

    def __init__(self) -> None:
        self.readings: list[dict[str, float] | None] = []

    def record_poller_counts(self, counts: dict[str, float] | None) -> None:
        self.readings.append(counts)


def _reader_that_stops(
    shutdown: asyncio.Event, outcome: Any, *, after: int = 1
) -> AsyncMock:
    """A poller-count reader that sets ``shutdown`` once it has been awaited ``after`` times.

    Event-driven, not wall-clock: the observer stops because the reader has
    produced the readings the test needs, so a slow runner cannot end the
    observer before its first tick. ``outcome`` is returned, or raised if it is
    an exception.
    """
    calls = 0

    async def _read() -> Any:
        nonlocal calls
        calls += 1
        if calls >= after:
            shutdown.set()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return AsyncMock(side_effect=_read)


class _HungTeardownWorker:
    """A worker whose teardown after a fatal error never returns.

    Models the incident shape the teardown watchdog exists for: temporalio
    cancels the body on a fatal poll error, but ``__aexit__`` then blocks
    forever instead of re-raising. ``release`` stands in for the process
    dying, so the test can end.
    """

    def __init__(self, *, on_fatal: Any, release: asyncio.Event) -> None:
        self.on_fatal = on_fatal
        self.release = release

    async def __aenter__(self) -> _HungTeardownWorker:
        task = asyncio.current_task()
        assert task is not None
        self.on_fatal(RuntimeError("Activity worker failed"))
        task.cancel()
        return self

    async def __aexit__(self, exc_type: Any, *args: Any) -> bool:
        await self.release.wait()
        if exc_type is asyncio.CancelledError:
            # What temporalio re-raises if its teardown ever does return.
            raise RuntimeError("Activity worker failed")
        return False


class TestFatalTeardownWatchdog:
    """The bound on temporalio's teardown after a fatal poll error."""

    def test_expiry_exits_nonzero(self) -> None:
        exited = threading.Event()
        codes: list[int] = []

        def exit_fn(code: int) -> None:
            codes.append(code)
            exited.set()

        watchdog = _FatalTeardownWatchdog(0.05, exit_fn=exit_fn)
        watchdog.arm()

        assert exited.wait(timeout=2)
        assert codes == [1]

    def test_disarm_before_expiry_prevents_exit(self) -> None:
        exit_fn = MagicMock()
        watchdog = _FatalTeardownWatchdog(30, exit_fn=exit_fn)
        watchdog.arm()
        timer = watchdog._timer
        assert timer is not None
        watchdog.disarm()

        # A cancelled timer's thread ends without running its callback; one
        # disarm failed to cancel would still be waiting out its 30s here.
        timer.join(timeout=5)
        assert not timer.is_alive()
        exit_fn.assert_not_called()
        assert not watchdog.armed

    def test_expiry_already_running_when_disarmed_does_not_exit(self) -> None:
        """Timer.cancel() cannot stop a callback already in flight."""
        exit_fn = MagicMock()
        watchdog = _FatalTeardownWatchdog(30, exit_fn=exit_fn)
        watchdog.arm()
        timer = watchdog._timer
        assert timer is not None
        watchdog.disarm()

        # The timer thread entered its callback just before disarm ran.
        timer.function(*timer.args)

        exit_fn.assert_not_called()

    def test_stale_expiry_from_an_earlier_arm_does_not_exit(self) -> None:
        exit_fn = MagicMock()
        watchdog = _FatalTeardownWatchdog(30, exit_fn=exit_fn)
        watchdog.arm()
        stale = watchdog._timer
        assert stale is not None
        watchdog.disarm()
        watchdog.arm()

        stale.function(*stale.args)

        exit_fn.assert_not_called()
        assert watchdog.armed
        watchdog.disarm()

    def test_non_positive_timeout_disables(self) -> None:
        watchdog = _FatalTeardownWatchdog(0, exit_fn=MagicMock())
        watchdog.arm()
        assert not watchdog.armed

    def test_second_arm_keeps_the_first_deadline(self) -> None:
        watchdog = _FatalTeardownWatchdog(30, exit_fn=MagicMock())
        watchdog.arm()
        first = watchdog._timer
        watchdog.arm()
        assert watchdog._timer is first
        watchdog.disarm()

    def test_default_bound_is_graceful_timeout_plus_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_WORKER_FATAL_TEARDOWN_TIMEOUT_SECONDS", raising=False)
        settings = MagicMock(graceful_shutdown_timeout_seconds=3600)
        with patch(
            "application_sdk.execution.settings.load_execution_settings",
            return_value=settings,
        ):
            assert _FatalTeardownWatchdog.from_settings()._timeout_seconds == 3660

    def test_env_overrides_the_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_WORKER_FATAL_TEARDOWN_TIMEOUT_SECONDS", "120")
        assert _FatalTeardownWatchdog.from_settings()._timeout_seconds == 120

    async def test_supervisor_disarms_once_the_teardown_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_RESTART_BACKOFF_CAP_SECONDS",
            0,
        )
        shutdown = asyncio.Event()
        exit_fn = MagicMock()
        watchdog = _FatalTeardownWatchdog(30, exit_fn=exit_fn)
        armed_during_teardown: list[bool] = []
        calls = {"n": 0}

        def on_exit() -> None:
            armed_during_teardown.append(watchdog.armed)

        def build() -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                watchdog.arm()  # what the on_fatal_error hook does
                return _CancelThenFailWorker(on_exit=on_exit)
            return _FakeWorker(fail=False, on_enter=shutdown.set)

        await _run_worker_with_restart(
            build_worker=build,
            shutdown_event=shutdown,
            teardown_watchdog=watchdog,
        )

        assert armed_during_teardown == [True]
        assert not watchdog.armed
        assert calls["n"] == 2
        exit_fn.assert_not_called()

    async def test_hung_teardown_exits_the_process(self) -> None:
        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        release = asyncio.Event()
        codes: list[int] = []

        def exit_fn(code: int) -> None:
            codes.append(code)
            # Stand-in for the process going away: unblock the test.
            loop.call_soon_threadsafe(shutdown.set)
            loop.call_soon_threadsafe(release.set)

        watchdog = _FatalTeardownWatchdog(0.1, exit_fn=exit_fn)

        def on_fatal(exc: BaseException) -> None:
            watchdog.arm()

        await asyncio.wait_for(
            _run_worker_with_restart(
                build_worker=lambda: _HungTeardownWorker(
                    on_fatal=on_fatal, release=release
                ),
                shutdown_event=shutdown,
                teardown_watchdog=watchdog,
            ),
            timeout=5,
        )

        assert codes == [1]


class TestObserveWorkerPollState:
    """Tests for the poll-state observer (ARUN-1127 instrumentation).

    The observer exists to identify the failure mechanism on a live tenant, so
    the contract under test is that it *reports* and never *acts*: no raise, no
    restart, and ``None`` (unreadable) is never collapsed into zero.
    """

    async def test_disabled_interval_returns_immediately(self) -> None:
        """A non-positive interval disables the observer entirely."""
        health = _RecordingHealthServer()
        shutdown = asyncio.Event()

        await asyncio.wait_for(
            _observe_worker_poll_state(
                shutdown_event=shutdown, health_server=health, interval=0
            ),
            timeout=1.0,
        )

        assert health.readings == []

    async def test_zero_pollers_warns_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero reading is the zombie signature: warn, record, never raise."""
        health = _RecordingHealthServer()
        shutdown = asyncio.Event()
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts",
            _reader_that_stops(shutdown, {"workflow_task": 0.0, "activity_task": 0.0}),
        )

        with patch(
            "application_sdk.execution._temporal.supervisor.logger"
        ) as mock_logger:
            await asyncio.wait_for(
                _observe_worker_poll_state(
                    shutdown_event=shutdown, health_server=health, interval=0.01
                ),
                timeout=2.0,
            )

        assert {"workflow_task": 0.0, "activity_task": 0.0} in health.readings
        assert mock_logger.warning.called

    async def test_zero_pollers_warns_once_per_park_not_per_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sustained zero state warns on the transition, not every interval.

        A parked worker emitting one WARNING per interval would bury the very
        ``poll loop failed fatally`` line this instrumentation exists to surface,
        so the zero branch is transition-gated like the unknown branch.
        """
        health = _RecordingHealthServer()
        shutdown = asyncio.Event()
        zero = {"workflow_task": 0.0, "activity_task": 0.0}

        # Event-driven, not wall-clock: the mocked reader itself sets shutdown
        # once it has been awaited twice, so the sustained park spans multiple
        # ticks by construction and the test cannot race a slow CI runner.
        calls = 0

        async def _read_then_stop():
            nonlocal calls
            calls += 1
            if calls >= 2:
                shutdown.set()
            return zero

        reader = AsyncMock(side_effect=_read_then_stop)
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts",
            reader,
        )

        with patch(
            "application_sdk.execution._temporal.supervisor.logger"
        ) as mock_logger:
            await asyncio.wait_for(
                _observe_worker_poll_state(
                    shutdown_event=shutdown, health_server=health, interval=0.01
                ),
                timeout=2.0,
            )

        # Multiple ticks ran during the sustained park (the reader drove shutdown
        # only after its second call), so a single tick could not have produced
        # the count below — the transition-gating is genuinely exercised.
        assert reader.await_count >= 2
        zero_warnings = [
            call
            for call in mock_logger.warning.call_args_list
            if "0 active pollers" in str(call)
        ]
        assert len(zero_warnings) == 1

    async def test_unknown_reading_is_not_reported_as_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable gauge records None (unknown), never a zero count."""
        health = _RecordingHealthServer()
        shutdown = asyncio.Event()
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts",
            _reader_that_stops(shutdown, None, after=2),
        )

        await asyncio.wait_for(
            _observe_worker_poll_state(
                shutdown_event=shutdown, health_server=health, interval=0.01
            ),
            timeout=2.0,
        )

        assert health.readings
        assert all(reading is None for reading in health.readings)

    async def test_read_failure_does_not_stop_the_observer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising reader is swallowed — an observer never takes the worker down."""
        shutdown = asyncio.Event()
        # Stops only after a second read: the observer survived the first raise.
        reader = _reader_that_stops(shutdown, RuntimeError("boom"), after=2)
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts", reader
        )

        # Must return cleanly on shutdown despite every read raising.
        await asyncio.wait_for(
            _observe_worker_poll_state(
                shutdown_event=shutdown, health_server=None, interval=0.01
            ),
            timeout=2.0,
        )
        assert reader.await_count == 2

    async def test_supervisor_starts_and_stops_the_observer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The supervisor owns the observer's lifetime, across worker rebuilds."""
        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_POLL_DIAGNOSTIC_INTERVAL_SECONDS",
            0.01,
        )
        health = _RecordingHealthServer()
        shutdown = asyncio.Event()
        # The observer's own reading ends the run, so the supervisor cannot
        # stop before the observer has ticked alongside the worker.
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts",
            _reader_that_stops(shutdown, {"workflow_task": 2.0}),
        )

        def build() -> _FakeWorker:
            return _FakeWorker(fail=False)

        await asyncio.wait_for(
            _run_worker_with_restart(
                build_worker=build,
                shutdown_event=shutdown,
                health_server=health,
            ),
            timeout=3.0,
        )

        assert health.readings, "observer never ran alongside the worker"
        # Stopped with the supervisor: no observer task outlives its return.
        assert not [
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__name__", "") == "_observe_worker_poll_state"
        ]


class TestObserverFeedsHealthEventGate:
    """The observer is what closes the gate on token_refresh health events."""

    @pytest.fixture(autouse=True)
    def _clean_gate(self):
        from application_sdk.execution._temporal.poll_state import worker_poll_state

        worker_poll_state.reset()
        yield
        worker_poll_state.reset()

    async def _run_observer(
        self, monkeypatch: pytest.MonkeyPatch, reading: dict[str, float] | None
    ) -> None:
        """Run the observer until it has taken two ``reading``s, then stop."""
        shutdown = asyncio.Event()
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts",
            _reader_that_stops(shutdown, reading, after=2),
        )
        await asyncio.wait_for(
            _observe_worker_poll_state(
                shutdown_event=shutdown,
                health_server=_RecordingHealthServer(),
                interval=0.01,
            ),
            timeout=2.0,
        )

    async def test_sustained_zero_closes_the_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A confirmed-parked worker stops advertising itself as healthy."""
        from application_sdk.execution._temporal.poll_state import worker_poll_state

        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_ZERO_POLLER_READINGS_BEFORE_STALE",
            1,
        )
        await self._run_observer(
            monkeypatch, {"workflow_task": 0.0, "activity_task": 0.0}
        )

        assert worker_poll_state.should_emit_health_event() is False

    async def test_unknown_readings_leave_the_gate_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable gauge must never mute a worker that may be healthy."""
        from application_sdk.execution._temporal.poll_state import worker_poll_state

        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_ZERO_POLLER_READINGS_BEFORE_STALE",
            1,
        )
        await self._run_observer(monkeypatch, None)

        assert worker_poll_state.should_emit_health_event() is True

    async def test_observation_exception_reopens_a_closed_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed reading after suppression must fail open, not stay muted.

        The observer except path used to log-and-continue without recording
        ``unknown``, so a parked worker that then hit a transient metrics-read
        failure stayed suppressed for the rest of the process lifetime.
        """
        from application_sdk.execution._temporal.poll_state import worker_poll_state

        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_ZERO_POLLER_READINGS_BEFORE_STALE",
            1,
        )

        calls = 0
        shutdown = asyncio.Event()

        async def _zero_then_raise():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"workflow_task": 0.0, "activity_task": 0.0}
            # The raising path is the one under test: stop once it has run.
            shutdown.set()
            raise RuntimeError("metrics unavailable")

        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.read_core_poller_counts",
            AsyncMock(side_effect=_zero_then_raise),
        )

        await asyncio.wait_for(
            _observe_worker_poll_state(
                shutdown_event=shutdown,
                health_server=_RecordingHealthServer(),
                interval=0.01,
            ),
            timeout=2.0,
        )

        assert calls == 2
        assert worker_poll_state.should_emit_health_event() is True

    async def test_supervisor_reopens_the_gate_on_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker must never be born muted by its predecessor's parked state."""
        from application_sdk.execution._temporal.poll_state import worker_poll_state

        monkeypatch.setattr(
            "application_sdk.execution._temporal.supervisor._WORKER_POLL_DIAGNOSTIC_INTERVAL_SECONDS",
            0,
        )
        worker_poll_state.configure(zero_readings_before_stale=1)
        worker_poll_state.record("zero")
        assert worker_poll_state.should_emit_health_event() is False

        shutdown = asyncio.Event()
        await _run_worker_with_restart(
            build_worker=lambda: _FakeWorker(fail=False, on_enter=shutdown.set),
            shutdown_event=shutdown,
        )

        assert worker_poll_state.should_emit_health_event() is True
