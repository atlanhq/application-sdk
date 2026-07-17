"""Tests for run_in_process native-crash containment (CNCT-85).

The functions submitted to the pool are module-level: the spawn child pickles
them by reference and re-imports this module, so anything they need must be
importable — no closures, no mocks.
"""

import ctypes
import time
from concurrent.futures.process import BrokenProcessPool

import pytest

from application_sdk.execution.heartbeat import run_in_process


def _echo(value: str) -> str:
    return f"echo:{value}"


def _segfault() -> None:
    ctypes.string_at(0)  # guaranteed SIGSEGV: read from address 0


def _sleep_forever() -> None:
    time.sleep(3600)


@pytest.mark.asyncio
async def test_returns_result_across_process_boundary():
    assert await run_in_process(_echo, "hi") == "echo:hi"


@pytest.mark.asyncio
async def test_native_crash_surfaces_as_broken_pool_and_parent_survives():
    """A segfault in the child must become a catchable Python exception here."""
    with pytest.raises(BrokenProcessPool):
        await run_in_process(_segfault)


@pytest.mark.asyncio
async def test_pool_recycles_after_native_crash():
    with pytest.raises(BrokenProcessPool):
        await run_in_process(_segfault)
    assert await run_in_process(_echo, "recovered") == "echo:recovered"


@pytest.mark.asyncio
async def test_timeout_kills_hung_child_and_pool_recycles():
    with pytest.raises(TimeoutError):
        await run_in_process(_sleep_forever, timeout=0.5)
    assert await run_in_process(_echo, "recovered") == "echo:recovered"
