"""The event-loop stall watchdog ends a worker whose loop stops running.

The scenarios that end a process run ``run_worker_mode`` in a child
(``tests/unit/loop_stall_probe.py``) and read its exit code and stderr; the
rest drive ``LoopStallWatchdog`` in-process against a private faulthandler.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import application_sdk._loop_stall_watchdog as watchdog_mod
from application_sdk._loop_stall_watchdog import LoopStallWatchdog
from application_sdk.main import _PostDrainExitArm
from tests.unit.private_stdlib import private_stdlib

_REPO_ROOT = Path(__file__).resolve().parents[2]
# A hang guard only: a stalled child exits within seconds, and a healthy one
# finishes on its own schedule. Generous because it covers interpreter start-up.
_CHILD_HANG_GUARD_SECONDS = 120


def _run_probe(
    block: str, *, timeout: str, shutdown_after: float, linger: float = 0
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
        watchdog = LoopStallWatchdog(300)

        watchdog.start()
        await asyncio.sleep(0)
        assert arm.call_args_list == [_timer_call(300)]

        watchdog.stop()
        cancel.assert_called_once_with()
        assert watchdog._arm() is False
        assert arm.call_count == 1

    async def test_cancelled_task_cancels_the_timer(self, fake_faulthandler) -> None:
        """A loop torn down without a drain (an exception, asyncio.run's
        cleanup) still leaves no timer behind."""
        _, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300)
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
        assert LoopStallWatchdog(timeout)._rearm_seconds == rearm

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


class TestForkHooks:
    """The hooks os.register_at_fork runs around every fork in the process."""

    def test_parent_rearms_for_what_is_left_of_the_loop_deadline(
        self, fake_faulthandler, clock
    ) -> None:
        """A thread that keeps forking cannot postpone a stopped loop's exit."""
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300)
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
        watchdog = LoopStallWatchdog(300)
        watchdog._arm()

        clock[0] += 301
        watchdog._pause_for_fork()
        watchdog._resume_after_fork()

        assert arm.call_args_list[-1] == _timer_call(0.001)

    def test_child_side_leaves_the_timer_off(self, fake_faulthandler) -> None:
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300)
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
        watchdog = LoopStallWatchdog(300)

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
