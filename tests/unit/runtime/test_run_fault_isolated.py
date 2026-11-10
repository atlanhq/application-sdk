"""Tests for run_fault_isolated (mechanism) and run_best_effort (policy) — CNCT-85.

The functions submitted to the pool are module-level: the spawn child pickles
them by reference and re-imports this module, so anything they need must be
importable — no closures, no mocks.
"""

import asyncio
import faulthandler
import time
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import MagicMock

import pytest

from application_sdk._runtime import cgroup as runtime_cgroup
from application_sdk._runtime import offload
from application_sdk._runtime.offload import run_best_effort, run_fault_isolated

# Every test here spawns pool children; see addopts in pyproject.toml.
pytestmark = pytest.mark.xdist_group("procpool")


def _echo(value: str) -> str:
    return f"echo:{value}"


def _segfault() -> None:
    # Not ctypes.string_at(0): on Windows ctypes converts the access violation
    # to OSError instead of dying. faulthandler's test hook faults for real on
    # every platform.
    faulthandler._sigsegv()


def _sleep_forever() -> None:
    time.sleep(3600)


def _raise_value_error() -> None:
    raise ValueError("boom")


def _pid() -> int:
    import os

    return os.getpid()


# ---------------------------------------------------------------------------
# run_fault_isolated — the mechanism: isolates, and RAISES on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_result_across_process_boundary():
    assert await run_fault_isolated(_echo, "hi") == "echo:hi"


@pytest.mark.asyncio
async def test_native_crash_surfaces_as_broken_pool_and_parent_survives():
    """A segfault in the child must become a catchable Python exception here."""
    with pytest.raises(BrokenProcessPool):
        await run_fault_isolated(_segfault)


@pytest.mark.asyncio
async def test_pool_recycles_after_native_crash():
    with pytest.raises(BrokenProcessPool):
        await run_fault_isolated(_segfault)
    assert await run_fault_isolated(_echo, "recovered") == "echo:recovered"


@pytest.mark.asyncio
async def test_timeout_kills_hung_child_and_pool_recycles():
    with pytest.raises(TimeoutError):
        await run_fault_isolated(_sleep_forever, timeout=0.5)
    assert await run_fault_isolated(_echo, "recovered") == "echo:recovered"


@pytest.mark.asyncio
async def test_foreign_discard_never_cancels_a_concurrent_caller():
    """A caller whose child is discarded by *another* caller's timeout must see
    a catchable exception, never CancelledError.

    The pool is now multi-worker (concurrent decode is allowed), so both calls
    run at once. Both hang; the first has a short timeout and, on firing,
    discards the shared pool — killing the second's still-running child. The
    second must surface as BrokenProcessPool (catchable), never CancelledError
    (a BaseException that would escape a warn-only except-Exception guard). The
    second's own long timeout never fires. (On a single-core box the pool is
    1-wide and the second is queued rather than running, but the required
    outcome — BrokenProcessPool, not CancelledError — is identical.)
    """
    hung, foreign = await asyncio.gather(
        run_fault_isolated(_sleep_forever, timeout=0.5),
        run_fault_isolated(_sleep_forever, timeout=30),
        return_exceptions=True,
    )
    assert isinstance(hung, TimeoutError)
    assert not isinstance(foreign, asyncio.CancelledError)
    assert isinstance(foreign, BrokenProcessPool)


# ---------------------------------------------------------------------------
# max_workers — the concurrency lever (default parallel; opt-in serialise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_workers_1_serialises_into_a_single_child():
    # Opt-in sequentialisation: a width-1 pool has exactly one child, so
    # concurrent max_workers=1 callers all run in that same child (serialised),
    # provably the same PID — no parallel decode.
    pids = await asyncio.gather(
        *(run_fault_isolated(_pid, max_workers=1) for _ in range(4))
    )
    assert len(set(pids)) == 1


@pytest.mark.asyncio
async def test_max_workers_below_one_is_rejected():
    with pytest.raises(ValueError):
        await run_fault_isolated(_pid, max_workers=0)


# ---------------------------------------------------------------------------
# run_best_effort — the policy: isolates, and SWALLOWS failure (logs + None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_best_effort_returns_result_and_does_not_warn():
    logger = MagicMock()
    assert await run_best_effort(_echo, "hi", label="Echo", logger=logger) == "echo:hi"
    logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_best_effort_swallows_native_crash():
    logger = MagicMock()
    assert await run_best_effort(_segfault, label="Decode", logger=logger) is None
    logger.warning.assert_called_once()
    assert "subprocess died" in logger.warning.call_args.args[0]
    assert "Decode" in logger.warning.call_args.args  # label interpolated


@pytest.mark.asyncio
async def test_best_effort_swallows_timeout():
    logger = MagicMock()
    result = await run_best_effort(
        _sleep_forever, label="Scan", logger=logger, timeout=0.5
    )
    assert result is None
    logger.warning.assert_called_once()
    assert "timed out" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_best_effort_swallows_ordinary_exception_with_traceback():
    logger = MagicMock()
    assert (
        await run_best_effort(_raise_value_error, label="Scan", logger=logger) is None
    )
    logger.warning.assert_called_once()
    # WARNING with a traceback (exc_info=True), not logger.exception.
    assert logger.warning.call_args.kwargs.get("exc_info") is True


# ---------------------------------------------------------------------------
# Idle reaping (FND-4278): children exist only while there is work
# ---------------------------------------------------------------------------
#
# Each test uses a width no other test in this module uses, so the pool it
# inspects is the one it created.

_REAP_DEADLINE_SECONDS = 15.0


def _sleep_for(seconds: float) -> float:
    time.sleep(seconds)
    return seconds


async def _wait_until_reaped(pool: offload._IsolationPool) -> None:
    """Wait until ``pool``'s idle shutdown has joined every child.

    Takes the pool object, not its width: the reaper drops the pool from the
    registry *before* joining the children, so "no longer registered" is not yet
    "reaped". Waits on ``reaped`` rather than joining the children here, because
    the reaper's ``shutdown(wait=True)`` already reaps them, and a second
    ``waitpid`` racing it gets ``ECHILD``, which ``multiprocessing`` reads as
    "still running".
    """
    deadline = time.monotonic() + _REAP_DEADLINE_SECONDS
    while not pool.reaped.is_set():
        assert time.monotonic() < deadline, "the idle pool was never reaped"
        await asyncio.sleep(0.05)
    assert all(p is not pool for p in offload._PROCESS_POOLS.values())


async def test_an_idle_pool_shuts_down_and_its_children_exit(monkeypatch):
    """The FND-4278 leak: before, the pool and its children lived forever."""
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "0.1")
    width = 7
    await asyncio.gather(
        *(run_fault_isolated(_sleep_for, 0.2, max_workers=width) for _ in range(3))
    )
    pool = offload._PROCESS_POOLS[width]
    children = list(pool.executor._processes.values())
    assert children

    await _wait_until_reaped(pool)

    # Every child has exited: the reaper's join recorded each exit code.
    assert all(child.exitcode is not None for child in children)
    # The next call at that width gets a fresh pool and still works.
    assert await run_fault_isolated(_echo, "again", max_workers=width) == "echo:again"


async def test_a_pool_is_reused_while_calls_keep_arriving(monkeypatch):
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "30")
    width = 6
    await run_fault_isolated(_echo, "one", max_workers=width)
    pool = offload._PROCESS_POOLS[width]
    await run_fault_isolated(_echo, "two", max_workers=width)
    assert offload._PROCESS_POOLS[width] is pool
    offload._discard_process_pool(width, pool)


async def test_a_call_still_in_flight_keeps_the_pool_alive(monkeypatch):
    """Going idle is "no child working", not "no caller waiting" on one call."""
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "0.05")
    width = 5
    long_call = asyncio.ensure_future(
        run_fault_isolated(_sleep_for, 1.5, max_workers=width)
    )
    assert await run_fault_isolated(_echo, "short", max_workers=width) == "echo:short"
    pool = offload._PROCESS_POOLS[width]

    await asyncio.sleep(0.5)  # ten idle periods, with the long call still running
    assert offload._PROCESS_POOLS.get(width) is pool

    assert await long_call == 1.5
    await _wait_until_reaped(pool)


async def test_a_cancelled_caller_does_not_reap_its_running_child(monkeypatch):
    """Cancelling the await leaves the child working; the pool must outlive it."""
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "0.05")
    width = 8
    call = asyncio.ensure_future(run_fault_isolated(_sleep_for, 1.0, max_workers=width))
    await asyncio.sleep(0.3)
    pool = offload._PROCESS_POOLS[width]
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await asyncio.sleep(0.3)
    assert offload._PROCESS_POOLS.get(width) is pool
    assert pool.in_flight == 1

    await _wait_until_reaped(pool)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 30.0),
        ("0", 0.0),
        ("12.5", 12.5),
        ("-1", 30.0),
        ("soon", 30.0),
        ("inf", 30.0),
    ],
)
def test_idle_seconds_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, raw)
    assert offload._idle_seconds() == expected


# ---------------------------------------------------------------------------
# Default width follows the container's CPU limit, not the node's cores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("cpus", "expected"), [(1, 1), (2, 2), (4, 4), (64, 4)])
def test_default_width_is_capped_by_available_cpus(monkeypatch, cpus, expected):
    monkeypatch.setattr(offload, "available_cpus", lambda: cpus)
    assert offload._default_process_pool_max_workers.__wrapped__() == expected


@pytest.mark.parametrize(("cpus", "expected"), [(1, 5), (4, 8), (28, 32), (64, 32)])
def test_blocking_pool_width_follows_available_cpus(monkeypatch, cpus, expected):
    """``sdk-blocking-`` is ``min(32, cpus + 4)`` of the pod's CPUs, not the node's.

    Before FND-4646 a 1-CPU pod on a 64-core node got 32 threads.
    """
    monkeypatch.setattr(offload, "available_cpus", lambda: cpus)
    monkeypatch.setattr(offload.os, "cpu_count", lambda: 64)
    assert offload._blocking_pool_width() == expected


def _write(tmp_path, name: str, contents: str) -> str:
    path = tmp_path / name
    path.write_text(contents)
    return str(path)


@pytest.mark.parametrize(
    ("cpu_max", "expected"),
    [("150000 100000", 1), ("50000 100000", 1), ("300000 100000", 3)],
)
def test_available_cpus_floors_the_quota(tmp_path, monkeypatch, cpu_max, expected):
    monkeypatch.setattr(
        runtime_cgroup, "CPU_MAX_V2", _write(tmp_path, "cpu.max", cpu_max)
    )
    monkeypatch.setattr(runtime_cgroup, "_affinity_count", lambda: 64)
    assert runtime_cgroup.available_cpus() == expected


def test_available_cpus_without_a_quota_is_the_affinity_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime_cgroup, "CPU_MAX_V2", _write(tmp_path, "cpu.max", "max 100000")
    )
    monkeypatch.setattr(runtime_cgroup, "CPU_QUOTA_V1", str(tmp_path / "absent"))
    monkeypatch.setattr(runtime_cgroup, "_affinity_count", lambda: 3)
    assert runtime_cgroup.available_cpus() == 3


def test_available_cpus_never_exceeds_affinity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime_cgroup, "CPU_MAX_V2", _write(tmp_path, "cpu.max", "800000 100000")
    )
    monkeypatch.setattr(runtime_cgroup, "_affinity_count", lambda: 2)
    assert runtime_cgroup.available_cpus() == 2


# ---------------------------------------------------------------------------
# fresh_child=True: a single-use child per call, never shared
# ---------------------------------------------------------------------------


def _pid_after(seconds: float) -> int:
    import os

    time.sleep(seconds)
    return os.getpid()


async def test_fresh_calls_back_to_back_each_get_a_new_child(monkeypatch):
    """The point of fresh_child=True: a pooled child would serve all three calls,
    carrying each one's peak heap into the next (the idle period never elapses
    between back-to-back steps)."""
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "30")
    pids = [await run_fault_isolated(_pid, fresh_child=True) for _ in range(3)]
    assert len(set(pids)) == 3


async def test_fresh_child_exits_as_soon_as_its_call_ends(monkeypatch):
    """The single-use pool is shut down and its child joined once the call ends.

    Keeps a reference to the pool: an unreferenced ProcessPoolExecutor stops
    its workers when it is garbage-collected, which would let this pass even
    if fresh_child never retired the pool itself. ``reaped`` is set only by that
    retirement. Checked through the pool rather than by probing the PID,
    because ``os.kill(pid, 0)`` is ``TerminateProcess`` on Windows.
    """
    # An idle period far longer than the deadline: only fresh_child's own retirement
    # can shut the pool down in time.
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "3600")
    created: list[offload._IsolationPool] = []

    class _RecordingPool(offload._IsolationPool):
        def __init__(self, max_workers: int) -> None:
            super().__init__(max_workers)
            created.append(self)

    monkeypatch.setattr(offload, "_IsolationPool", _RecordingPool)

    assert await run_fault_isolated(_echo, "once", fresh_child=True) == "echo:once"

    # reaped is set only after shutdown(wait=True) has joined every child.
    (pool,) = created
    await _wait_until_reaped(pool)


async def test_fresh_never_touches_the_shared_width_one_pool(monkeypatch):
    monkeypatch.setenv(offload.FAULT_ISOLATION_IDLE_SECONDS_ENV, "30")
    shared_pid = await run_fault_isolated(_pid, max_workers=1)
    shared = offload._PROCESS_POOLS[1]

    fresh_pid = await run_fault_isolated(_pid, fresh_child=True, max_workers=1)
    with pytest.raises(TimeoutError):
        await run_fault_isolated(_sleep_forever, timeout=0.5, fresh_child=True)

    assert fresh_pid != shared_pid
    assert offload._PROCESS_POOLS.get(1) is shared
    assert await run_fault_isolated(_pid, max_workers=1) == shared_pid
    offload._discard_process_pool(1, shared)


async def test_fresh_calls_run_concurrently_on_separate_children():
    a, b = await asyncio.gather(
        run_fault_isolated(_pid_after, 0.5, fresh_child=True),
        run_fault_isolated(_pid_after, 0.5, fresh_child=True),
    )
    assert a != b


async def test_fresh_native_crash_surfaces_as_broken_pool():
    with pytest.raises(BrokenProcessPool):
        await run_fault_isolated(_segfault, fresh_child=True)
    assert await run_fault_isolated(_echo, "next", fresh_child=True) == "echo:next"


async def test_fresh_propagates_ordinary_exceptions():
    with pytest.raises(ValueError, match="boom"):
        await run_fault_isolated(_raise_value_error, fresh_child=True)


@pytest.mark.parametrize("width", [0, 2, 4])
async def test_fresh_rejects_a_width_other_than_one(width):
    with pytest.raises(ValueError, match="max_workers"):
        await run_fault_isolated(_echo, "x", fresh_child=True, max_workers=width)


def _echo_fresh(*, fresh: str) -> str:
    return f"fresh:{fresh}"


async def test_a_workers_own_fresh_kwarg_is_still_forwarded():
    """The option is named fresh_child so it never captures a ``fresh`` kwarg
    that callers forward to ``func``, as they could before it existed."""
    assert await run_fault_isolated(_echo_fresh, fresh="yes") == "fresh:yes"
