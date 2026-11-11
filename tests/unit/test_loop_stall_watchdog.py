"""The event-loop stall watchdog ends a worker whose loop stops running.

The scenarios that end a process run ``run_worker_mode`` in a child
(``tests/unit/loop_stall_probe.py``) and read its exit code and stderr; the
rest drive ``_LoopStallWatchdog`` in-process against a private faulthandler.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import application_sdk.main as main_mod
from application_sdk.main import _LoopStallWatchdog, _PostDrainExitArm
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
        private_stdlib(main_mod, "faulthandler") as local_fh,
        private_stdlib(main_mod, "os") as local_os,
        patch.object(local_fh, "dump_traceback_later") as arm,
        patch.object(local_fh, "cancel_dump_traceback_later") as cancel,
        patch.object(local_os, "register_at_fork"),
    ):
        yield arm, cancel


class TestWatchdogInProcess:
    async def test_zero_timeout_arms_nothing(
        self, fake_faulthandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        arm, _ = fake_faulthandler
        monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", "0")
        watchdog = _LoopStallWatchdog.from_settings()

        watchdog.start()
        await asyncio.sleep(0)

        arm.assert_not_called()

    async def test_arms_with_exit_and_stops_rearming_once_stopped(
        self, fake_faulthandler
    ) -> None:
        arm, cancel = fake_faulthandler
        watchdog = _LoopStallWatchdog(300)

        watchdog.start()
        await asyncio.sleep(0)
        arm.assert_called_once_with(300, repeat=False, file=2, exit=True)

        watchdog.stop()
        cancel.assert_called_once_with()
        assert watchdog._arm() is False
        assert arm.call_count == 1

    async def test_cancelled_task_cancels_the_timer(self, fake_faulthandler) -> None:
        """A loop torn down without a drain (an exception, asyncio.run's
        cleanup) still leaves no timer behind."""
        _, cancel = fake_faulthandler
        watchdog = _LoopStallWatchdog(300)
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
        assert _LoopStallWatchdog(timeout)._rearm_seconds == rearm

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
            watchdog = _LoopStallWatchdog.from_settings()
        assert watchdog._timeout_seconds == expected


class TestHandoffToPostDrainDeadline:
    def test_drain_on_shutdown_stops_the_watchdog(self) -> None:
        event = asyncio.Event()
        event.set()
        watchdog = MagicMock(spec=_LoopStallWatchdog)
        arm = _PostDrainExitArm(
            enabled=True, shutdown_event=event, stall_watchdog=watchdog
        )

        with patch("application_sdk.main._start_post_drain_exit_deadline") as start:
            arm.on_worker_drained()

        watchdog.stop.assert_called_once_with()
        start.assert_called_once_with()

    def test_drain_for_a_supervised_restart_keeps_it_running(self) -> None:
        """A worker rebuilt after a fatal error can poll again, so stay armed."""
        watchdog = MagicMock(spec=_LoopStallWatchdog)
        arm = _PostDrainExitArm(
            enabled=True, shutdown_event=asyncio.Event(), stall_watchdog=watchdog
        )

        with patch("application_sdk.main._start_post_drain_exit_deadline"):
            arm.on_worker_drained()

        watchdog.stop.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX-only")
def test_forked_child_exits_normally_while_armed() -> None:
    """A child forked while the timer is armed can still finish its interpreter
    exit, and the parent is re-armed afterwards. Without the fork hooks the
    child waits forever on a timer thread it never inherited."""
    script = textwrap.dedent(
        """
        import asyncio, os, sys, warnings
        warnings.simplefilter("ignore", DeprecationWarning)
        from application_sdk.main import _LoopStallWatchdog

        async def main():
            watchdog = _LoopStallWatchdog(60)
            watchdog.start()
            await asyncio.sleep(0)
            pid = os.fork()
            if pid == 0:
                sys.exit(0)
            _, status = os.waitpid(pid, 0)
            print("child finished, rearmed", watchdog._armed)
            watchdog.stop()

        asyncio.run(main())
        """
    )
    result = subprocess.run(  # noqa: S603 — fixed argv: this interpreter + a literal script
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_CHILD_HANG_GUARD_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # The child's own exit code reflects SDK at-exit hooks that do not survive
    # a fork; the property here is that its exit finishes at all.
    assert "child finished, rearmed True" in result.stdout
