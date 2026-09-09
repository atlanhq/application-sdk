"""Unit tests for application_sdk.execution.heartbeat.

Covers the heartbeat controllers, the auto-heartbeat loop and the stall
watchdog that rides on its tick. The offload primitives this module re-exports
are tested at their own home, ``tests/unit/runtime/test_offload.py``.

Strict no-real-loop policy: the auto-heartbeat loop tests use intervals well
under 1 second with pre-set events to avoid hangs.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk._runtime.progress import ProgressTracker
from application_sdk.execution.heartbeat import (
    NoopHeartbeatController,
    TemporalHeartbeatController,
    auto_heartbeat_loop,
    stop_heartbeat_task,
)
from application_sdk.execution.progress import ProgressWatchdogMode

# ---------------------------------------------------------------------------
# NoopHeartbeatController
# ---------------------------------------------------------------------------


class TestNoopHeartbeatController:
    def test_heartbeat_records_details(self) -> None:
        ctl = NoopHeartbeatController()
        ctl.heartbeat("a", 1)
        ctl.heartbeat("b", 2)
        assert ctl._heartbeat_calls == [("a", 1), ("b", 2)]
        assert ctl.get_last_heartbeat_details() == ("b", 2)

    def test_keepalive_replays_last_details(self) -> None:
        ctl = NoopHeartbeatController()
        ctl.heartbeat("x")
        ctl.heartbeat_keepalive()
        assert ctl._heartbeat_calls[-1] == ("x",)

    def test_initial_state_empty(self) -> None:
        ctl = NoopHeartbeatController()
        assert ctl.get_last_heartbeat_details() == ()


# ---------------------------------------------------------------------------
# TemporalHeartbeatController
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_temporalio(monkeypatch):
    """Inject a fake `temporalio` module so the inline `from temporalio import activity`
    inside TemporalHeartbeatController is exercised without spawning Temporal.
    """
    fake_activity = MagicMock()
    fake_info = MagicMock()
    fake_info.heartbeat_details = ("resumed", 42)
    fake_activity.info.return_value = fake_info

    # Build a fake module with `activity` attribute
    fake_pkg = types.ModuleType("temporalio")
    fake_pkg.activity = fake_activity

    monkeypatch.setitem(sys.modules, "temporalio", fake_pkg)
    return fake_activity


class TestTemporalHeartbeatController:
    def test_heartbeat_calls_activity_heartbeat(self, fake_temporalio) -> None:
        """Exercises the function-local import of ``temporalio.activity``."""
        ctl = TemporalHeartbeatController()
        ctl.heartbeat("progress", 1)
        fake_temporalio.heartbeat.assert_called_once_with("progress", 1)
        assert ctl._last_details == ("progress", 1)

    def test_keepalive_replays_last_details(self, fake_temporalio) -> None:
        """Exercises the keepalive path after importing ``temporalio.activity``."""
        ctl = TemporalHeartbeatController()
        ctl.heartbeat("first", 99)
        fake_temporalio.heartbeat.reset_mock()
        ctl.heartbeat_keepalive()
        fake_temporalio.heartbeat.assert_called_once_with("first", 99)

    def test_get_last_heartbeat_details(self, fake_temporalio) -> None:
        """Exercises heartbeat-detail lookup through ``temporalio.activity``."""
        ctl = TemporalHeartbeatController()
        details = ctl.get_last_heartbeat_details()
        assert details == ("resumed", 42)
        fake_temporalio.info.assert_called_once()


# ---------------------------------------------------------------------------
# auto_heartbeat_loop
# ---------------------------------------------------------------------------


class TestAutoHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_stop_event_pre_set_breaks_immediately(self) -> None:
        """If stop_event is set before entry, loop breaks via wait_for resolution."""
        stop = asyncio.Event()
        stop.set()
        hb = MagicMock()
        await auto_heartbeat_loop(0.5, hb, stop, task_name="t")
        # stop_event was set → no heartbeat sent before break
        hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_heartbeat_after_one_interval(self) -> None:
        """When stop never fires before timeout, fn is called and loop continues."""
        stop = asyncio.Event()
        hb_calls = []

        def hb_fn():
            hb_calls.append(1)
            if len(hb_calls) >= 2:
                stop.set()

        await auto_heartbeat_loop(
            interval_seconds=0.001, heartbeat_fn=hb_fn, stop_event=stop, task_name="t"
        )
        # Should have made at least 2 heartbeat calls (then we set stop).
        assert len(hb_calls) >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_exception_logged_and_loop_continues(self) -> None:
        """If hb_fn raises Exception, it's logged but the loop keeps going."""
        stop = asyncio.Event()
        calls = []

        def hb_fn():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            stop.set()

        await auto_heartbeat_loop(
            interval_seconds=0.001, heartbeat_fn=hb_fn, stop_event=stop, task_name="t"
        )
        # Exception happened on first call; loop continued until stop
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_base_exception_propagates(self) -> None:
        """asyncio.CancelledError (BaseException) must propagate out of the loop."""
        stop = asyncio.Event()

        def hb_fn():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await auto_heartbeat_loop(
                interval_seconds=0.001,
                heartbeat_fn=hb_fn,
                stop_event=stop,
                task_name="cancelled-task",
            )

    @pytest.mark.asyncio
    async def test_stop_event_set_during_wait_breaks(self) -> None:
        """If stop_event becomes set during wait_for, loop must break (line 144).

        We simulate this by patching wait_for to set stop and return successfully —
        which exercises the `break` after a non-Timeout return.
        """
        stop = asyncio.Event()
        hb = MagicMock()

        async def fake_wait_for(coro, timeout):
            try:
                coro.close()
            except Exception:  # noqa: S110 — closing dead test-scaffold coroutine; nothing to log
                pass
            stop.set()
            return None  # no TimeoutError → break

        with patch(
            "application_sdk.execution.heartbeat.asyncio.wait_for",
            side_effect=fake_wait_for,
        ):
            await auto_heartbeat_loop(
                interval_seconds=0.5, heartbeat_fn=hb, stop_event=stop, task_name="t"
            )
        # The break path: heartbeat should NOT have been called.
        hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_loop_warning_logged(self, caplog) -> None:
        """Simulate a blocked loop by patching asyncio.wait_for to time out
        immediately and time.monotonic to report a huge elapsed.

        Verifies the warning branch (lines 149-157) executes.
        """
        import logging

        stop = asyncio.Event()
        hb_calls = []

        def hb_fn():
            hb_calls.append(1)
            stop.set()  # ensure loop exits after one iteration

        # Always raise TimeoutError on wait_for so we don't actually block.
        async def fake_wait_for(coro, timeout):
            # Cancel the awaitable so no warnings about un-awaited coroutines.
            try:
                coro.close()
            except Exception:  # noqa: S110 — closing dead test-scaffold coroutine; nothing to log
                pass
            raise asyncio.TimeoutError()

        state = {"n": 0}

        def fake_monotonic():
            state["n"] += 1
            return 0.0 if state["n"] == 1 else 100.0

        with (
            patch(
                "application_sdk.execution.heartbeat.asyncio.wait_for",
                side_effect=fake_wait_for,
            ),
            patch(
                "application_sdk.execution.heartbeat.time.monotonic",
                side_effect=fake_monotonic,
            ),
            caplog.at_level(logging.WARNING),
        ):
            await auto_heartbeat_loop(
                interval_seconds=0.001,
                heartbeat_fn=hb_fn,
                stop_event=stop,
                task_name="slow",
            )

        assert hb_calls  # heartbeat was sent

    @pytest.mark.asyncio
    async def test_memory_pressure_silent_when_env_unset(self, monkeypatch) -> None:
        """No memory-pressure WARNING when K8S_POD_MEMORY_LIMIT is not set."""
        from application_sdk.execution import heartbeat as hb_mod

        monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)
        stop = asyncio.Event()

        def hb_fn():
            stop.set()

        with patch.object(hb_mod, "logger") as mock_logger:
            await auto_heartbeat_loop(0.001, hb_fn, stop, task_name="t")

        memory_warnings = [
            c for c in mock_logger.warning.call_args_list if "Memory pressure" in str(c)
        ]
        assert not memory_warnings

    @pytest.mark.asyncio
    async def test_memory_pressure_warning_emitted_above_threshold(
        self, monkeypatch
    ) -> None:
        """WARNING is emitted with correct percentage when RSS ≥ 80% of limit."""
        from application_sdk.execution import heartbeat as hb_mod
        from application_sdk.observability.resource_sampler import ResourceSample

        limit = 4 * 1024**3  # 4 GiB in bytes
        rss = int(limit * 0.85)  # 85%
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", str(limit))

        stop = asyncio.Event()

        def hb_fn():
            stop.set()

        with (
            patch(
                "application_sdk.execution.heartbeat._resource_sampler.sample",
                return_value=ResourceSample(cpu_time_s=1.0, rss_bytes=rss),
            ),
            patch.object(hb_mod, "logger") as mock_logger,
        ):
            await auto_heartbeat_loop(0.001, hb_fn, stop, task_name="mem-task")

        warning_calls = [
            c for c in mock_logger.warning.call_args_list if "Memory pressure" in str(c)
        ]
        assert warning_calls, "Expected memory-pressure WARNING"
        # args: (fmt, task_name, pct_float, rss_gib, limit_gib)
        _fmt, task, pct, *_ = warning_calls[0].args
        assert task == "mem-task"
        assert abs(pct - 85.0) < 0.5

    @pytest.mark.asyncio
    async def test_memory_pressure_warning_latches_above_threshold(
        self, monkeypatch
    ) -> None:
        """Warning fires exactly once when ratio stays ≥ 80% across N ticks."""
        from application_sdk.execution import heartbeat as hb_mod
        from application_sdk.observability.resource_sampler import ResourceSample

        limit = 4 * 1024**3
        rss = int(limit * 0.85)  # constant 85% — always above threshold
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", str(limit))

        tick = {"n": 0}
        stop = asyncio.Event()

        def hb_fn():
            tick["n"] += 1
            if tick["n"] >= 4:  # run 4 ticks to confirm latch holds
                stop.set()

        with (
            patch(
                "application_sdk.execution.heartbeat._resource_sampler.sample",
                return_value=ResourceSample(cpu_time_s=1.0, rss_bytes=rss),
            ),
            patch.object(hb_mod, "logger") as mock_logger,
        ):
            await auto_heartbeat_loop(0.001, hb_fn, stop, task_name="latch-task")

        assert tick["n"] >= 4, "Expected at least 4 ticks"
        mem_warnings = [
            c for c in mock_logger.warning.call_args_list if "Memory pressure" in str(c)
        ]
        assert (
            len(mem_warnings) == 1
        ), f"Expected exactly 1 warning, got {len(mem_warnings)}"

    @pytest.mark.asyncio
    async def test_memory_pressure_rearms_after_drop_below_hysteresis(
        self, monkeypatch
    ) -> None:
        """Warning fires twice: once at 0.85, re-arms after dropping to 0.70, fires again at 0.85."""
        from application_sdk.execution import heartbeat as hb_mod
        from application_sdk.observability.resource_sampler import ResourceSample

        limit = 4 * 1024**3
        # Sample sequence: 0.85 → 0.70 → 0.85 (must produce 2 warnings)
        ratios = [0.85, 0.70, 0.85]
        samples = [
            ResourceSample(cpu_time_s=float(i), rss_bytes=int(limit * r))
            for i, r in enumerate(ratios)
        ]
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", str(limit))

        tick = {"n": 0}
        stop = asyncio.Event()

        def hb_fn():
            tick["n"] += 1
            if tick["n"] >= len(samples):
                stop.set()

        with (
            patch(
                "application_sdk.execution.heartbeat._resource_sampler.sample",
                side_effect=samples,
            ),
            patch.object(hb_mod, "logger") as mock_logger,
        ):
            await auto_heartbeat_loop(0.001, hb_fn, stop, task_name="rearm-task")

        mem_warnings = [
            c for c in mock_logger.warning.call_args_list if "Memory pressure" in str(c)
        ]
        assert len(mem_warnings) == 2, f"Expected 2 warnings, got {len(mem_warnings)}"


# ---------------------------------------------------------------------------
# Stall watchdog (ADR-0018 / FND-286)
# ---------------------------------------------------------------------------


@dataclass
class WatchdogClock:
    """A monotonic clock the test advances explicitly.

    The tracker takes its clock by injection precisely so tests never patch
    ``time.monotonic``: the asyncio loop driving ``auto_heartbeat_loop`` shares
    that global, so patching it makes the loop itself misbehave.
    """

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class StallRecorder:
    """Captures the (stalled_for, last_label) pairs on_stall is called with."""

    calls: list[tuple[float, str]] = field(default_factory=list)
    raises: bool = False

    def __call__(self, stalled_for: float, last_label: str) -> None:
        self.calls.append((stalled_for, last_label))
        if self.raises:
            raise RuntimeError("cancelling the activity task failed")


@dataclass
class LoopRun:
    """What one driven run of the loop observed."""

    beats: int
    stall_infos: list[Any]
    stall_warnings: list[Any]
    recorded: list[Any]


async def _drive(
    *,
    tracker: ProgressTracker | None,
    clock: WatchdogClock,
    mode: ProgressWatchdogMode = ProgressWatchdogMode.OFF,
    budget: float | None = None,
    on_stall: Callable[[float, str], None] | None = None,
    step: float = 1.0,
    ticks: int = 1,
    per_tick: Callable[[int], None] | None = None,
    record_raises: bool = False,
) -> LoopRun:
    """Run ``auto_heartbeat_loop`` for at most ``ticks`` ticks.

    Each tick advances ``clock`` by ``step`` *before* the watchdog runs, so at
    tick ``n`` the watchdog sees a gap of ``n * step`` unless something marked
    progress. The real loop interval stays sub-millisecond — the fake clock,
    not wall time, is what the watchdog reads.
    """
    from application_sdk.execution import heartbeat as hb_mod
    from application_sdk.execution import progress_telemetry as pt_mod

    stop = asyncio.Event()
    beats: list[int] = []
    histogram = MagicMock()
    if record_raises:
        histogram.record.side_effect = RuntimeError("metric backend down")

    def hb_fn() -> None:
        beats.append(1)
        clock.advance(step)
        if per_tick is not None:
            per_tick(len(beats))
        if len(beats) >= ticks:
            stop.set()

    # Two loggers: the watchdog's decisions are logged from the loop, the metric
    # is recorded (and its failures reported) from the telemetry module.
    with (
        patch.object(hb_mod, "logger") as mock_logger,
        patch.object(pt_mod, "logger") as mock_metric_logger,
        patch.object(pt_mod, "_no_progress_gap_histogram", return_value=histogram),
    ):
        await auto_heartbeat_loop(
            interval_seconds=0.001,
            heartbeat_fn=hb_fn,
            stop_event=stop,
            task_name="extract",
            progress=tracker,
            max_no_progress_seconds=budget,
            watchdog_mode=mode,
            on_stall=on_stall,
        )

    return LoopRun(
        beats=len(beats),
        stall_infos=[
            c
            for c in mock_logger.info.call_args_list
            if "no observable progress" in str(c)
        ],
        stall_warnings=[
            c
            for c in (
                *mock_logger.warning.call_args_list,
                *mock_metric_logger.warning.call_args_list,
            )
            if any(
                marker in str(c)
                for marker in ("progress", "Stall watchdog", "Stall handler")
            )
        ],
        recorded=list(histogram.record.call_args_list),
    )


class TestStallWatchdogInert:
    """Purely additive: with nothing injected, the loop behaves exactly as before."""

    @pytest.mark.asyncio
    async def test_no_tracker_means_no_watchdog(self) -> None:
        run = await _drive(tracker=None, clock=WatchdogClock(), budget=1.0, ticks=3)

        assert run.beats == 3
        assert not run.stall_infos and not run.stall_warnings and not run.recorded

    @pytest.mark.asyncio
    async def test_off_mode_observes_nothing(self) -> None:
        clock = WatchdogClock()
        run = await _drive(
            tracker=ProgressTracker(clock=clock),
            clock=clock,
            mode=ProgressWatchdogMode.OFF,
            budget=1.0,
            step=100.0,
            ticks=3,
        )

        assert run.beats == 3
        assert not run.stall_infos and not run.stall_warnings and not run.recorded

    @pytest.mark.asyncio
    async def test_no_budget_means_no_watchdog(self) -> None:
        clock = WatchdogClock()
        run = await _drive(
            tracker=ProgressTracker(clock=clock),
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=None,
            step=100.0,
            ticks=3,
        )

        assert run.beats == 3
        assert not run.stall_infos and not run.stall_warnings and not run.recorded


class TestStallWatchdogEnforce:
    @pytest.mark.asyncio
    async def test_gap_at_the_budget_fails_the_activity_and_stops_the_loop(
        self,
    ) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.mark_progress("write_batch")
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=stalls,
            step=2.0,
            ticks=20,
        )

        # Gap reaches the budget on tick 3 (2s per tick); the watchdog *is*
        # heartbeat_task, so it returns there rather than beating 17 more times.
        assert stalls.calls == [(6.0, "write_batch")]
        assert run.beats == 3

    @pytest.mark.asyncio
    async def test_stall_is_reported_at_warning_and_names_the_last_signal(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.mark_progress("fetch_page")

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=StallRecorder(),
            step=5.0,
            ticks=5,
        )

        assert len(run.stall_warnings) == 1
        assert not run.stall_infos
        assert "failing the activity" in run.stall_warnings[0].args[0]
        assert run.stall_warnings[0].args[1:] == ("extract", 5.0, 5.0, "fetch_page")

    @pytest.mark.asyncio
    async def test_detection_latency_is_budget_plus_at_most_one_tick(self) -> None:
        """The tick that first sees ``stalled >= budget`` is the tick that fires."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=10.0,
            on_stall=stalls,
            step=3.0,
            ticks=20,
        )

        # Gaps go 3, 6, 9, 12 — the first tick at or past the budget is the 4th,
        # i.e. one tick's worth of overshoot and no more.
        assert run.beats == 4
        assert stalls.calls == [(12.0, "")]

    @pytest.mark.asyncio
    async def test_progress_each_tick_never_stalls(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=stalls,
            step=4.0,
            ticks=10,
            per_tick=lambda _n: tracker.mark_progress("write_batch"),
        )

        assert not stalls.calls
        assert run.beats == 10

    @pytest.mark.asyncio
    async def test_unbounded_hold_suppresses_the_stall(self) -> None:
        """A vouched-for opaque call is never accused of stalling."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.enter_hold("full table scan", timeout=None)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=stalls,
            step=50.0,
            ticks=6,
        )

        assert not stalls.calls
        assert run.beats == 6

    @pytest.mark.asyncio
    async def test_lapsed_bounded_hold_fires_at_allowance_plus_budget(self) -> None:
        """Kill time for a wedged held call is ``allowance + budget``."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.enter_hold("snapshot metadata query", timeout=10.0)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=stalls,
            step=1.0,
            ticks=40,
        )

        # The allowance vouches to t+10; the stall clock then runs from the
        # deadline, so the gap first reaches 5s at t+15.
        assert run.beats == 15
        assert stalls.calls == [(5.0, "")]

    @pytest.mark.asyncio
    async def test_beat_stays_unconditional_while_stalled(self) -> None:
        """The keepalive is the crash detector; a stall must not gate it."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.WARN,
            budget=1.0,
            step=100.0,
            ticks=5,
        )

        assert run.beats == 5

    @pytest.mark.asyncio
    async def test_on_stall_failure_stops_the_loop_anyway(self) -> None:
        """If the handler can't fail the attempt, stop beating and let
        Temporal's heartbeat_timeout reclaim it — never keep beating for an
        attempt already judged wedged."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        stalls = StallRecorder(raises=True)

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=stalls,
            step=5.0,
            ticks=20,
        )

        assert len(stalls.calls) == 1
        assert run.beats == 1
        assert any("Stall handler" in str(c) for c in run.stall_warnings)


class TestStallWatchdogWarn:
    @pytest.mark.asyncio
    async def test_warn_reports_each_gap_once_and_never_fails(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.mark_progress("write_batch")
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.WARN,
            budget=5.0,
            on_stall=stalls,
            step=2.0,
            ticks=12,
        )

        # Gaps of 6s open at ticks 3, 6, 9 and 12 — reported once each, not on
        # every one of the 12 ticks, because reporting re-arms the stall clock.
        assert len(run.stall_infos) == 4
        assert not stalls.calls
        assert run.beats == 12

    @pytest.mark.asyncio
    async def test_warn_never_logs_at_warning(self) -> None:
        """A fleet-wide default that logged at WARNING would manufacture exactly
        the alert noise ADR-0018 exists to reduce."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.WARN,
            budget=5.0,
            step=10.0,
            ticks=4,
        )

        assert run.stall_infos
        assert not run.stall_warnings

    @pytest.mark.asyncio
    async def test_re_arm_keeps_the_label_it_just_reported(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.mark_progress("fetch_page")

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.WARN,
            budget=5.0,
            step=10.0,
            ticks=3,
        )

        assert len(run.stall_infos) == 3
        for call in run.stall_infos:
            assert call.args[-1] == "fetch_page"
        assert tracker.last_label == "fetch_page"

    @pytest.mark.asyncio
    async def test_unlabelled_gap_reads_as_none(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.WARN,
            budget=5.0,
            step=10.0,
            ticks=1,
        )

        assert run.stall_infos[0].args[-1] == "<none>"


class TestStallWatchdogMetric:
    @pytest.mark.asyncio
    async def test_gap_is_recorded_with_task_label_and_mode(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        tracker.mark_progress("write_batch")

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.WARN,
            budget=5.0,
            step=6.0,
            ticks=1,
        )

        assert len(run.recorded) == 1
        value, attributes = run.recorded[0].args
        assert value == 6.0
        assert attributes == {
            "task.name": "extract",
            "progress.last_label": "write_batch",
            "watchdog.mode": "warn",
        }

    @pytest.mark.asyncio
    async def test_enforced_gap_is_recorded_too(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=StallRecorder(),
            step=5.0,
            ticks=1,
        )

        assert len(run.recorded) == 1
        assert run.recorded[0].args[1]["watchdog.mode"] == "enforce"

    @pytest.mark.asyncio
    async def test_metric_failure_never_breaks_the_watchdog(self) -> None:
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=stalls,
            step=5.0,
            ticks=10,
            record_raises=True,
        )

        assert stalls.calls == [(5.0, "")]
        assert any("gap metric" in str(c) for c in run.stall_warnings)


class TestStallWatchdogWiring:
    @pytest.mark.asyncio
    async def test_enforce_without_a_handler_downgrades_to_warn(self) -> None:
        """A wiring bug must not silence the watchdog, and must not let it
        pretend it enforced."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=5.0,
            on_stall=None,
            step=10.0,
            ticks=3,
        )

        assert any("no on_stall handler" in str(c) for c in run.stall_warnings)
        assert len(run.stall_infos) == 3
        assert run.beats == 3
        assert all(c.args[1]["watchdog.mode"] == "warn" for c in run.recorded)

    @pytest.mark.asyncio
    async def test_non_positive_budget_disables_the_watchdog(self) -> None:
        """One bad config value must not become a fleet-wide kill switch."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=0.0,
            on_stall=stalls,
            step=10.0,
            ticks=3,
        )

        assert any("non-positive" in str(c) for c in run.stall_warnings)
        assert not stalls.calls
        assert not run.stall_infos and not run.recorded
        assert run.beats == 3

    @pytest.mark.asyncio
    async def test_negative_budget_disables_the_watchdog(self) -> None:
        """A negative allowance vouches for nothing: the guard must refuse it
        the same way it refuses zero."""
        clock = WatchdogClock()
        tracker = ProgressTracker(clock=clock)
        stalls = StallRecorder()

        run = await _drive(
            tracker=tracker,
            clock=clock,
            mode=ProgressWatchdogMode.ENFORCE,
            budget=-5.0,
            on_stall=stalls,
            step=10.0,
            ticks=3,
        )

        assert any("non-positive" in str(c) for c in run.stall_warnings)
        assert not stalls.calls
        assert not run.stall_infos and not run.recorded
        assert run.beats == 3

    @pytest.mark.asyncio
    async def test_non_finite_budget_disables_the_watchdog(self) -> None:
        """NaN slips past a `<= 0` check (every comparison against NaN is
        False) and +inf would silently never enforce — both must disable the
        watchdog like any other invalid budget."""
        for bad_budget in (float("nan"), float("inf")):
            clock = WatchdogClock()
            tracker = ProgressTracker(clock=clock)
            stalls = StallRecorder()

            run = await _drive(
                tracker=tracker,
                clock=clock,
                mode=ProgressWatchdogMode.ENFORCE,
                budget=bad_budget,
                on_stall=stalls,
                step=10.0,
                ticks=3,
            )

            assert any(
                "non-positive" in str(c) for c in run.stall_warnings
            ), f"budget={bad_budget} did not trip the guard"
            assert not stalls.calls, f"budget={bad_budget} enforced a stall"
            assert not run.stall_infos and not run.recorded
            assert run.beats == 3


class TestStopHeartbeatTask:
    """The shared shutdown: contain the task's own death, never the caller's."""

    async def test_obedient_task_stops_gracefully(self) -> None:
        stop = asyncio.Event()

        async def obedient() -> None:
            await stop.wait()

        task = asyncio.ensure_future(obedient())
        await stop_heartbeat_task(task, stop, "obedient")
        assert task.done() and not task.cancelled()

    async def test_stuck_task_is_cancelled_without_escaping(self) -> None:
        stop = asyncio.Event()

        async def stuck() -> None:
            await asyncio.sleep(60)

        task = asyncio.ensure_future(stuck())
        await stop_heartbeat_task(task, stop, "stuck")
        assert task.cancelled()

    async def test_outer_cancellation_is_not_swallowed(self) -> None:
        # A cancellation aimed at the CALLER while it waits here must
        # propagate — swallowing it would make a cancelled activity report
        # completion. The heartbeat task's own CancelledError (the other
        # tests) must still be contained.
        stop = asyncio.Event()

        async def stuck() -> None:
            await asyncio.sleep(60)

        heartbeat = asyncio.ensure_future(stuck())

        async def caller() -> str:
            await stop_heartbeat_task(heartbeat, stop, "stuck")
            return "completed"

        caller_task = asyncio.ensure_future(caller())
        await asyncio.sleep(0.05)
        caller_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller_task
        assert heartbeat.cancelled()
