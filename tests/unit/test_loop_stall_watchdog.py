"""The event-loop stall watchdog ends a shutting-down worker whose loop stopped.

The scenarios that end a process run ``run_worker_mode`` in a child
(``tests/unit/loop_stall_probe.py``), send it a real SIGTERM and read its exit
code and stderr; the rest drive ``LoopStallWatchdog`` in-process against a
private faulthandler and signal module. The bound is 3 s, so each timer runs
3.75 s (the bound plus one re-arm interval).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import application_sdk._loop_stall_watchdog as watchdog_mod
from application_sdk._loop_stall_watchdog import (
    LoopStallWatchdog,
    largest_bound_within,
    timer_seconds,
)
from application_sdk.constants import load_worker_loop_stall_timeout_override
from application_sdk.main import _PostDrainExitArm
from tests.unit.private_stdlib import private_stdlib

_REPO_ROOT = Path(__file__).resolve().parents[2]
# A hang guard only: a stalled child exits within seconds, and a healthy one
# finishes on its own schedule. Generous because it covers interpreter start-up.
_CHILD_HANG_GUARD_SECONDS = 120
_TIMER_HEADER = "Timeout (0:00:03.750000)!"

_posix_signals = pytest.mark.skipif(
    sys.platform == "win32", reason="asyncio has no signal handlers on Windows"
)


def _run_probe(
    block: str,
    *,
    when: str,
    sigterm_after: float,
    drain: float = 0,
    linger: float = 0,
    timeout: str = "3",
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
            "--when",
            when,
            "--sigterm-after",
            str(sigterm_after),
            "--drain",
            str(drain),
            "--linger",
            str(linger),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_CHILD_HANG_GUARD_SECONDS,
        check=False,
    )


@_posix_signals
class TestStalledShutdownExits:
    """A shutdown whose event loop stops running exits with every thread's stack."""

    @pytest.mark.parametrize(
        ("block", "culprit"),
        [("sleep", "_block_on_sleep"), ("lock", "_block_on_lock")],
    )
    def test_loop_frozen_before_sigterm(self, block: str, culprit: str) -> None:
        """The loop never runs its shutdown handler; the process-level handler
        arms the timer from inside the blocked call."""
        result = _run_probe(block, when="before-sigterm", sigterm_after=1.5)

        assert result.returncode == 1, result.stderr
        assert _TIMER_HEADER in result.stderr
        assert f"in {culprit}" in result.stderr
        assert "probe exited cleanly" not in result.stdout

    def test_loop_frozen_during_the_drain(self) -> None:
        result = _run_probe("sleep", when="during-drain", sigterm_after=1)

        assert result.returncode == 1, result.stderr
        assert _TIMER_HEADER in result.stderr
        assert "in _block_on_sleep" in result.stderr


@_posix_signals
class TestNothingElseIsKilled:
    def test_steady_state_block_far_past_the_bound_survives(self) -> None:
        """Before shutdown a frozen loop is the liveness probe's job."""
        result = _run_probe("sleep:7", when="steady", sigterm_after=8)

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout

    def test_drain_longer_than_the_bound_survives(self) -> None:
        """A draining loop keeps re-arming, and asyncio's own SIGTERM handling
        still drives the drain with the watchdog's handler chained in front."""
        result = _run_probe("none", when="steady", sigterm_after=1, drain=7)

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout

    def test_clean_shutdown_leaves_no_timer_armed(self) -> None:
        """The process outlives the timer after the worker stops."""
        result = _run_probe("none", when="steady", sigterm_after=1, linger=5)

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout

    def test_zero_turns_it_off(self) -> None:
        result = _run_probe(
            "sleep:5", when="before-sigterm", sigterm_after=1.5, timeout="0"
        )

        assert result.returncode == 0, result.stderr
        assert "Timeout (" not in result.stderr
        assert "probe exited cleanly" in result.stdout


@pytest.fixture
def fake_faulthandler(monkeypatch: pytest.MonkeyPatch):
    """faulthandler holds one timer per process; keep tests off the real one.
    The fork hooks are module state, so each test starts with none registered."""
    monkeypatch.setattr(watchdog_mod, "_fork_hooks_registered", False)
    monkeypatch.setattr(watchdog_mod, "_fork_target", None)
    with (
        private_stdlib(watchdog_mod, "faulthandler") as local_fh,
        private_stdlib(watchdog_mod, "os") as local_os,
        patch.object(local_fh, "dump_traceback_later") as arm,
        patch.object(local_fh, "cancel_dump_traceback_later") as cancel,
        # Windows has no register_at_fork; the watchdog then skips the hooks.
        patch.object(local_os, "register_at_fork", create=True),
    ):
        yield arm, cancel


@pytest.fixture
def fake_signal():
    """The watchdog's view of the signal module, with no real handlers set."""
    installed: dict[int, object] = {}
    previous = MagicMock(name="previous_handler")
    with (
        private_stdlib(watchdog_mod, "signal") as local_signal,
        patch.object(local_signal, "getsignal", return_value=previous),
        patch.object(
            local_signal,
            "signal",
            side_effect=lambda sig, handler: installed.__setitem__(sig, handler),
        ),
    ):
        yield installed, previous, local_signal


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


class TestShutdownArming:
    def test_installing_arms_nothing(self, fake_faulthandler, fake_signal) -> None:
        arm, _ = fake_faulthandler
        installed, _, _ = fake_signal

        LoopStallWatchdog(3).install((signal.SIGINT, signal.SIGTERM))

        assert set(installed) == {signal.SIGINT, signal.SIGTERM}
        arm.assert_not_called()

    def test_signal_arms_once_and_chains(self, fake_faulthandler, fake_signal) -> None:
        arm, _ = fake_faulthandler
        installed, previous, _ = fake_signal
        LoopStallWatchdog(3).install((signal.SIGTERM,))
        handler = installed[signal.SIGTERM]

        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)

        assert arm.call_args_list == [_timer_call(3.75)]
        assert previous.call_args_list == [call(signal.SIGTERM, None)] * 2

    def test_zero_installs_nothing(self, fake_faulthandler, fake_signal) -> None:
        installed, _, _ = fake_signal

        LoopStallWatchdog(0).install((signal.SIGTERM,))

        assert installed == {}

    def test_off_the_main_thread_leaves_arming_to_the_loop(
        self, fake_faulthandler, fake_signal
    ) -> None:
        _, _, local_signal = fake_signal
        local_signal.signal.side_effect = ValueError("main thread only")

        LoopStallWatchdog(3).install((signal.SIGTERM,))

    def test_stopped_watchdog_ignores_the_signal(self, fake_faulthandler) -> None:
        arm, _ = fake_faulthandler
        watchdog = LoopStallWatchdog(3)
        watchdog.stop()

        watchdog.on_shutdown_signal()

        arm.assert_not_called()

    async def test_loop_shutdown_handler_rearms_until_stopped(
        self, fake_faulthandler
    ) -> None:
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(3)

        watchdog.start_rearming()
        await asyncio.sleep(0)
        assert arm.call_args_list == [_timer_call(3.75), _timer_call(3.75)]

        watchdog.stop()
        cancel.assert_called_once_with()
        assert watchdog._arm() is False

    async def test_cancelled_task_cancels_the_timer(self, fake_faulthandler) -> None:
        """A loop torn down without a drain (an exception, asyncio.run's
        cleanup) still leaves no timer behind."""
        _, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(3)
        watchdog.start_rearming()
        await asyncio.sleep(0)

        assert watchdog._task is not None
        watchdog._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog._task

        cancel.assert_called_once_with()


class TestTimerArithmetic:
    @pytest.mark.parametrize(("bound", "timer"), [(3, 3.75), (60, 75), (300, 315)])
    def test_timer_adds_one_rearm_interval(self, bound: float, timer: float) -> None:
        assert timer_seconds(bound) == timer

    @pytest.mark.parametrize("seconds", [5, 28, 75, 400, 86370])
    def test_largest_bound_fills_the_limit(self, seconds: float) -> None:
        assert timer_seconds(largest_bound_within(seconds)) == pytest.approx(seconds)


class TestOverrideParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, None), ("", None), ("0", 0.0), ("45", 45.0)],
    )
    def test_valid(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float | None
    ) -> None:
        if raw is None:
            monkeypatch.delenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS")
        else:
            monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", raw)
        assert load_worker_loop_stall_timeout_override() == expected

    @pytest.mark.parametrize("raw", ["abc", "-5", "inf", "nan"])
    def test_invalid_falls_back_to_derived(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", raw)
        with pytest.warns(UserWarning):
            assert load_worker_loop_stall_timeout_override() is None


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
        assert arm.call_args_list == [_timer_call(315), _timer_call(115)]

    def test_deadline_passed_during_the_fork_fires_at_once(
        self, fake_faulthandler, clock
    ) -> None:
        arm, _ = fake_faulthandler
        watchdog = LoopStallWatchdog(300)
        watchdog._arm()

        clock[0] += 316
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

    def test_hooks_register_once_and_follow_the_latest_watchdog(
        self, fake_faulthandler
    ) -> None:
        """A process that runs the worker twice registers one set of hooks,
        and they act on the watchdog installed last."""
        arm, cancel = fake_faulthandler
        first, second = LoopStallWatchdog(300), LoopStallWatchdog(300)

        first._register_fork_hooks()
        second._register_fork_hooks()
        second._arm()
        watchdog_mod._pause_for_fork()
        watchdog_mod._resume_after_fork()

        assert watchdog_mod.os.register_at_fork.call_count == 1
        cancel.assert_called_once_with()
        assert arm.call_count == 2
        assert second._armed and not first._armed

    def test_a_fork_releases_the_watchdog_it_paused(self, fake_faulthandler) -> None:
        """Installing another watchdog mid-fork must not release the wrong lock."""
        first, second = LoopStallWatchdog(300), LoopStallWatchdog(300)
        first._register_fork_hooks()

        watchdog_mod._pause_for_fork()
        second._register_fork_hooks()
        watchdog_mod._resume_after_fork()

        assert first._lock.acquire(blocking=False)
        first._lock.release()
        assert second._lock.acquire(blocking=False)
        second._lock.release()

    def test_fork_while_unarmed_arms_nothing(self, fake_faulthandler) -> None:
        arm, cancel = fake_faulthandler
        watchdog = LoopStallWatchdog(300)

        watchdog._pause_for_fork()
        watchdog._resume_after_fork()

        arm.assert_not_called()
        cancel.assert_not_called()


class TestExitBy:
    """Re-arms during the drain never push the exit past the shutdown budget."""

    def test_freeze_late_in_the_drain_still_exits_by_the_deadline(
        self, fake_faulthandler, clock
    ) -> None:
        arm, _ = fake_faulthandler
        watchdog = LoopStallWatchdog(60, exit_by_seconds=100)

        watchdog.on_shutdown_signal()  # timer 75 s, within the 100 s
        clock[0] += 50
        watchdog._arm()  # 50 s left: capped
        clock[0] += 45
        watchdog._arm()  # 5 s left
        clock[0] += 10
        watchdog._arm()  # deadline passed: fires at once

        assert arm.call_args_list == [
            _timer_call(75),
            _timer_call(50),
            _timer_call(5),
            _timer_call(0.001),
        ]

    def test_without_a_deadline_each_rearm_gets_the_full_timer(
        self, fake_faulthandler, clock
    ) -> None:
        arm, _ = fake_faulthandler
        watchdog = LoopStallWatchdog(60)

        watchdog.on_shutdown_signal()
        clock[0] += 500
        watchdog._arm()

        assert arm.call_args_list == [_timer_call(75), _timer_call(75)]

    def test_override_past_the_deadline_is_cut_at_the_signal(
        self, fake_faulthandler, clock
    ) -> None:
        arm, _ = fake_faulthandler
        watchdog = LoopStallWatchdog(300, exit_by_seconds=28)

        watchdog.on_shutdown_signal()

        assert arm.call_args_list == [_timer_call(28)]


class TestExitDeadlineReport:
    """The loop says, once, when only the exit deadline can still end it, so
    the dump that follows is not read as a stall."""

    def test_reported_once_at_the_last_rearm_before_the_deadline(
        self, fake_faulthandler, clock
    ) -> None:
        reported = MagicMock()
        # Bound 60 s: re-arms every 15 s, so 15 s or less left is the last one.
        watchdog = LoopStallWatchdog(60, exit_by_seconds=100, on_exit_deadline=reported)

        watchdog.on_shutdown_signal()
        clock[0] += 50
        watchdog._arm()  # 50 s left: timer capped, but more re-arms to come
        reported.assert_not_called()
        clock[0] += 40
        watchdog._arm()  # 10 s left: the last re-arm
        clock[0] += 5
        watchdog._arm()  # 5 s left

        reported.assert_called_once_with(10)

    def test_short_grace_drain_capped_from_the_start_is_not_reported(
        self, fake_faulthandler, clock
    ) -> None:
        """On a 30 s grace the derived bound's timer is the whole limit, so
        every re-arm of a normal drain is capped; that alone must not report."""
        reported = MagicMock()
        bound = largest_bound_within(28)
        watchdog = LoopStallWatchdog(
            bound, exit_by_seconds=28, on_exit_deadline=reported
        )

        watchdog.on_shutdown_signal()
        clock[0] += 5.6
        watchdog._arm()  # 22.4 s left, below the full timer but not the last

        reported.assert_not_called()

    def test_signal_handler_never_reports(self, fake_faulthandler, clock) -> None:
        """A deadline already within one re-arm at the signal is reported by the
        loop's first re-arm, never from inside the signal handler."""
        reported = MagicMock()
        watchdog = LoopStallWatchdog(300, exit_by_seconds=10, on_exit_deadline=reported)

        watchdog.on_shutdown_signal()
        reported.assert_not_called()

        watchdog._arm()
        reported.assert_called_once_with(10)

    def test_without_a_deadline_nothing_is_reported(
        self, fake_faulthandler, clock
    ) -> None:
        reported = MagicMock()
        watchdog = LoopStallWatchdog(60, on_exit_deadline=reported)

        watchdog.on_shutdown_signal()
        clock[0] += 500
        watchdog._arm()

        reported.assert_not_called()


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
