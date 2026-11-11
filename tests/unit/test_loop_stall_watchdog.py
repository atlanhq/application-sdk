"""The event-loop stall watchdog ends a worker whose loop stops running.

The scenarios that end a process run ``run_worker_mode`` in a child
(``tests/unit/loop_stall_probe.py``) and read its exit code and stderr; the
rest drive ``LoopStallWatchdog`` in-process against a private faulthandler.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from temporalio.testing import ActivityEnvironment
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput

import application_sdk._loop_stall_watchdog as watchdog_mod
from application_sdk._loop_stall_watchdog import (
    LoopStallWatchdog,
    RunningHeartbeatTimeouts,
    rearm_interval_seconds,
)
from application_sdk.execution._temporal.interceptors.loop_stall import (
    LoopStallInterceptor,
)
from application_sdk.main import _PostDrainExitArm
from tests.unit.private_stdlib import private_stdlib

_REPO_ROOT = Path(__file__).resolve().parents[2]
# A hang guard only: a stalled child exits within seconds, and a healthy one
# finishes on its own schedule. Generous because it covers interpreter start-up.
_CHILD_HANG_GUARD_SECONDS = 120


def _run_probe(
    block: str,
    *,
    timeout: str,
    shutdown_after: float,
    linger: float = 0,
    phase: str = "worker",
    heartbeat: float = 0,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS": timeout,
        # Keeps the post-drain deadline out of the shutdown these tests time.
        "ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS": "0",
    }
    return subprocess.run(  # noqa: S603 — fixed argv: this interpreter + the in-repo probe module
        [
            sys.executable,
            "-m",
            "tests.unit.loop_stall_probe",
            block,
            str(shutdown_after),
            str(linger),
            "--phase",
            phase,
            "--heartbeat",
            str(heartbeat),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_CHILD_HANG_GUARD_SECONDS,
        check=False,
    )


class TestStalledLoopExits:
    """A worker whose loop stops running exits with every thread's stack."""

    @pytest.mark.parametrize(
        ("block", "culprit"),
        [("sleep", "_block_on_sleep"), ("lock", "_block_on_lock")],
    )
    def test_blocked_loop_exits_1_with_the_blocking_frame_on_stderr(
        self, block: str, culprit: str
    ) -> None:
        # The shutdown signal never comes: a blocked loop could not run it.
        result = _run_probe(block, timeout="1", shutdown_after=3600)

        assert result.returncode == 1, result.stderr
        assert "Timeout (0:00:01)!" in result.stderr
        assert f"in {culprit}" in result.stderr
        assert "probe exited cleanly" not in result.stdout


class TestHeartbeatAwareTimeout:
    """The bound is the longest heartbeat timeout among running activities,
    when that is longer than the configured one (2 s here)."""

    def test_stall_shorter_than_a_running_heartbeat_timeout_survives(self) -> None:
        result = _run_probe(
            "sleep:4", timeout="2", shutdown_after=7, phase="activity", heartbeat=6
        )

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout

    def test_stall_past_a_running_heartbeat_timeout_exits(self) -> None:
        result = _run_probe(
            "sleep", timeout="2", shutdown_after=3600, phase="activity", heartbeat=6
        )

        assert result.returncode == 1, result.stderr
        assert "Timeout (0:00:06)!" in result.stderr
        assert "in _block_on_sleep" in result.stderr

    def test_finished_activity_drops_back_to_the_configured_timeout(self) -> None:
        result = _run_probe(
            "sleep",
            timeout="2",
            shutdown_after=3600,
            phase="after-activity",
            heartbeat=6,
        )

        assert result.returncode == 1, result.stderr
        assert "Timeout (0:00:02)!" in result.stderr

    def test_activity_without_heartbeat_timeout_does_not_extend_it(self) -> None:
        result = _run_probe(
            "sleep", timeout="2", shutdown_after=3600, phase="activity", heartbeat=0
        )

        assert result.returncode == 1, result.stderr
        assert "Timeout (0:00:02)!" in result.stderr


class TestHealthyLoopSurvives:
    def test_ticking_loop_outlives_several_timeouts(self) -> None:
        result = _run_probe("none", timeout="2", shutdown_after=6)

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout

    def test_clean_shutdown_leaves_no_timer_armed(self) -> None:
        """The process outlives the timeout twice over after the worker stops."""
        result = _run_probe("none", timeout="2", shutdown_after=1, linger=4)

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout


@pytest.fixture
def fake_faulthandler():
    """faulthandler holds one timer per process; keep tests off the real one."""
    with (
        private_stdlib(watchdog_mod, "faulthandler") as local_fh,
        private_stdlib(watchdog_mod, "os") as local_os,
        patch.object(local_fh, "dump_traceback_later") as arm,
        patch.object(local_fh, "cancel_dump_traceback_later") as cancel,
        # Windows has no register_at_fork; start() then skips the fork hooks.
        patch.object(local_os, "register_at_fork", create=True),
    ):
        yield arm, cancel


@pytest.fixture
def clock():
    """The watchdog's monotonic clock, frozen and advanced by hand."""
    now = [1000.0]
    with (
        private_stdlib(watchdog_mod, "time") as local_time,
        patch.object(local_time, "monotonic", side_effect=lambda: now[0]),
    ):
        yield now


def _timer_call(seconds: float):
    return call(seconds, repeat=False, file=2, exit=True)


class TestWatchdogInProcess:
    async def test_zero_timeout_arms_nothing(
        self, fake_faulthandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        arm, _ = fake_faulthandler
        monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", "0")
        watchdog = LoopStallWatchdog.from_settings()

        watchdog.start()
        await asyncio.sleep(0)

        arm.assert_not_called()

    async def test_arms_with_exit_and_stops_rearming_once_stopped(
        self, fake_faulthandler
    ) -> None:
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300, running=RunningHeartbeatTimeouts())

        watchdog.start()
        await asyncio.sleep(0)
        assert arm.call_args_list == [_timer_call(300)]

        watchdog.stop()
        cancel.assert_called_once_with()
        assert watchdog._arm() is None
        assert arm.call_count == 1

    async def test_cancelled_task_cancels_the_timer(self, fake_faulthandler) -> None:
        """A loop torn down without a drain (an exception, asyncio.run's
        cleanup) still leaves no timer behind."""
        _, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300, running=RunningHeartbeatTimeouts())
        watchdog.start()
        await asyncio.sleep(0)

        assert watchdog._task is not None
        watchdog._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog._task

        cancel.assert_called_once_with()

    @pytest.mark.parametrize(("timeout", "rearm"), [(300, 15.0), (2, 0.5)])
    def test_rearms_several_times_per_timeout(
        self, timeout: float, rearm: float
    ) -> None:
        assert rearm_interval_seconds(timeout) == rearm

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 300.0), ("45", 45.0), ("-5", 0.0), ("abc", 300.0), ("inf", 300.0)],
    )
    def test_timeout_env_parsing(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
    ) -> None:
        if raw is None:
            monkeypatch.delenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS")
        else:
            monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", raw)
        warns = raw in ("abc", "inf")
        with pytest.warns(UserWarning) if warns else contextlib.nullcontext():
            watchdog = LoopStallWatchdog.from_settings()
        assert watchdog.timeout_seconds == expected


class TestRunningActivities:
    """How running activities move the timeout, in-process."""

    def test_effective_timeout_is_the_longer_of_the_two(self) -> None:
        running = RunningHeartbeatTimeouts()
        watchdog = LoopStallWatchdog(300, running=running)

        with running.track(60):
            assert watchdog.effective_timeout_seconds() == 300
            with running.track(3600), running.track(600):
                assert watchdog.effective_timeout_seconds() == 3600
            assert watchdog.effective_timeout_seconds() == 300
        with running.track(None), running.track(0):
            assert running.longest() == 0.0

    async def test_start_and_finish_rearm_at_once(self, fake_faulthandler) -> None:
        """The activity that starts may block the loop before the next tick,
        and a shrunk timeout must not wait out a sleep sized for the old one."""
        arm, _ = fake_faulthandler
        running = RunningHeartbeatTimeouts()
        changes: list[float] = []
        watchdog = LoopStallWatchdog(
            2, running=running, on_timeout_change=changes.append
        )
        watchdog.start()
        await asyncio.sleep(0)
        assert arm.call_args_list == [_timer_call(2)]

        with running.track(3600):
            assert arm.call_args_list[-1] == _timer_call(3600)
        assert arm.call_args_list[-1] == _timer_call(2)

        # The re-arm task was waiting out 15 s; it wakes and re-arms for 2 s.
        calls_before_wake = arm.call_count
        for _ in range(3):
            await asyncio.sleep(0)
        assert arm.call_count > calls_before_wake
        assert arm.call_args_list[-1] == _timer_call(2)
        assert changes == [3600, 2]

        watchdog.stop()
        assert running.on_change is None

    async def test_interceptor_tracks_the_activity_heartbeat_timeout(self) -> None:
        running = RunningHeartbeatTimeouts()
        seen: list[float] = []

        class _Body(ActivityInboundInterceptor):
            def __init__(self) -> None:
                pass

            async def execute_activity(self, input: ExecuteActivityInput) -> None:
                seen.append(running.longest())

        chain = LoopStallInterceptor(running).intercept_activity(_Body())
        activity_input = ExecuteActivityInput(
            fn=len, args=(), executor=None, headers={}
        )
        for heartbeat in (timedelta(seconds=90), None):
            env = ActivityEnvironment()
            env.info = dataclasses.replace(env.info, heartbeat_timeout=heartbeat)
            await env.run(chain.execute_activity, activity_input)

        assert seen == [90.0, 0.0]
        assert running.longest() == 0.0


class TestForkHooks:
    """The hooks os.register_at_fork runs around every fork in the process."""

    def test_parent_rearms_for_what_is_left_of_the_loop_deadline(
        self, fake_faulthandler, clock
    ) -> None:
        """A thread that keeps forking cannot postpone a stopped loop's exit."""
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300, running=RunningHeartbeatTimeouts())
        watchdog._arm()

        clock[0] += 200
        watchdog._pause_for_fork()
        watchdog._resume_after_fork()

        cancel.assert_called_once_with()
        assert arm.call_args_list == [_timer_call(300), _timer_call(100)]

    def test_deadline_passed_during_the_fork_fires_at_once(
        self, fake_faulthandler, clock
    ) -> None:
        arm, _ = fake_faulthandler
        watchdog = LoopStallWatchdog(300, running=RunningHeartbeatTimeouts())
        watchdog._arm()

        clock[0] += 301
        watchdog._pause_for_fork()
        watchdog._resume_after_fork()

        assert arm.call_args_list[-1] == _timer_call(0.001)

    def test_child_side_leaves_the_timer_off(self, fake_faulthandler) -> None:
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300, running=RunningHeartbeatTimeouts())
        watchdog._arm()

        watchdog._pause_for_fork()
        watchdog._release_after_fork()

        cancel.assert_called_once_with()
        assert arm.call_count == 1
        assert watchdog._armed is False
        # The child can still take the lock, so stop() there does not hang.
        watchdog.stop()

    def test_fork_while_unarmed_arms_nothing(self, fake_faulthandler) -> None:
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300, running=RunningHeartbeatTimeouts())

        watchdog._pause_for_fork()
        watchdog._resume_after_fork()

        arm.assert_not_called()
        cancel.assert_not_called()


class TestHandoffToPostDrainDeadline:
    def test_drain_on_shutdown_stops_the_watchdog(self) -> None:
        event = asyncio.Event()
        event.set()
        watchdog = MagicMock(spec=LoopStallWatchdog)
        arm = _PostDrainExitArm(
            enabled=True, shutdown_event=event, stall_watchdog=watchdog
        )

        with patch("application_sdk.main._start_post_drain_exit_deadline") as start:
            arm.on_worker_drained()

        watchdog.stop.assert_called_once_with()
        start.assert_called_once_with()

    def test_drain_for_a_supervised_restart_keeps_it_running(self) -> None:
        """A worker rebuilt after a fatal error can poll again, so stay armed."""
        watchdog = MagicMock(spec=LoopStallWatchdog)
        arm = _PostDrainExitArm(
            enabled=True, shutdown_event=asyncio.Event(), stall_watchdog=watchdog
        )

        with patch("application_sdk.main._start_post_drain_exit_deadline"):
            arm.on_worker_drained()

        watchdog.stop.assert_not_called()


# Imports only the watchdog module, so the process has no thread but the main
# one when it forks: a thread holding a lock at fork time (stderr's, a logging
# handler's) would leave the child stuck for reasons unrelated to the timer.
# No event loop either: a child cannot tear down a loop it inherited.
_FORK_SCRIPT = """
import os, sys, threading, time
from application_sdk._loop_stall_watchdog import LoopStallWatchdog

threads = [t.name for t in threading.enumerate()]
assert threads == ["MainThread"], threads
watchdog = LoopStallWatchdog(60)
watchdog._register_fork_hooks()
watchdog._arm()
pid = os.fork()
if pid == 0:
    sys.exit(0)
# A stuck child is reported and reaped, never left holding this test's pipes.
for _ in range(300):
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        print("child exit", os.waitstatus_to_exitcode(status), "rearmed", watchdog._armed)
        break
    time.sleep(0.1)
else:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    print("child stuck at exit")
watchdog.stop()
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX-only")
def test_forked_child_exits_normally_while_armed() -> None:
    """A child forked while the timer is armed finishes its interpreter exit,
    and the parent is re-armed. Without the fork hooks the child waits forever
    on a timer thread it never inherited."""
    result = subprocess.run(  # noqa: S603 — fixed argv: this interpreter + a literal script
        [sys.executable, "-c", textwrap.dedent(_FORK_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_CHILD_HANG_GUARD_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "child exit 0 rearmed True" in result.stdout, result.stdout
