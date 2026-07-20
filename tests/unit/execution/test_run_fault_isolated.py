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

from application_sdk.execution.heartbeat import run_best_effort, run_fault_isolated


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
async def test_foreign_timeout_discard_never_cancels_a_queued_caller():
    """A caller queued behind a timing-out one must see a catchable exception.

    With the single-worker pool, the second call is provably still queued when
    the first one's timeout discards the pool. That must surface to the second
    caller as BrokenProcessPool (catchable), never CancelledError (a
    BaseException that would escape a warn-only except-Exception guard).
    """
    hung, queued = await asyncio.gather(
        run_fault_isolated(_sleep_forever, timeout=0.5),
        run_fault_isolated(_echo, "queued"),
        return_exceptions=True,
    )
    assert isinstance(hung, TimeoutError)
    assert not isinstance(queued, asyncio.CancelledError)
    assert isinstance(queued, BrokenProcessPool)


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
