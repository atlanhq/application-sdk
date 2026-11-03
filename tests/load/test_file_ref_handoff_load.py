"""Opt-in benchmark: a same-pod 2 GiB FileReference hand-off never stalls the loop.

FND-4081 ``Done when``: no event-loop stall above 100 ms when a 2 GiB ref is
handed off on the same pod. Before the fix the materialise pre-check hashed the
whole file synchronously — measured at 3.5 s with a warm cache.

A ticker coroutine records the largest gap between its wake-ups while the
hand-off runs. The control arm hashes the same file on the loop, the way the
old pre-check did, and must show a stall: that proves the ticker can see one.

Run (excluded from every default/CI suite; see tests/load/README.md):

    uv run poe load-test-storage
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from application_sdk.contracts.base import Input
from application_sdk.contracts.types import FileReference
from application_sdk.storage import integrity
from application_sdk.storage.file_ref_sync import (
    has_refs_to_materialize,
    materialize_file_refs,
)
from application_sdk.storage.reference import persist_file_reference

from .conftest import write_source_file

pytestmark = pytest.mark.load

_HANDOFF_BYTES = 2 * 1024 * 1024 * 1024
_MAX_STALL_S = 0.100
_TICK_S = 0.005


class _HandOff(Input):
    ref: FileReference


async def _max_loop_gap(work: Callable[[], Awaitable[Any]]) -> float:
    """Run *work*; return the longest the loop went without running the ticker."""
    stop = asyncio.Event()
    worst = 0.0

    async def _ticker() -> None:
        nonlocal worst
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(_TICK_S)
            now = time.monotonic()
            worst = max(worst, now - last - _TICK_S)
            last = now

    ticker = asyncio.ensure_future(_ticker())
    await asyncio.sleep(_TICK_S * 2)  # ticker running before the work starts
    try:
        await work()
        # Let overdue timers fire before stopping, or the late tick is lost.
        await asyncio.sleep(_TICK_S * 2)
    finally:
        stop.set()
        await ticker
    return worst


@pytest.fixture
async def handed_off(local_store, tmp_path: Path) -> tuple[Any, Path, FileReference]:
    """A 2 GiB file persisted on this pod, as the producing task leaves it."""
    local = tmp_path / "handoff.bin"
    write_source_file(local, _HANDOFF_BYTES)
    ref = await persist_file_reference(local_store, FileReference.from_local(local))
    return local_store, local, ref


async def test_trusted_handoff_does_not_stall_the_loop(handed_off) -> None:
    store, _, ref = handed_off
    inp = _HandOff(ref=ref)

    async def _handoff() -> None:
        if has_refs_to_materialize(inp):
            await materialize_file_refs(store, inp)

    gap = await _max_loop_gap(_handoff)
    print(f"\n[load] trusted 2 GiB hand-off: worst loop gap {gap * 1000:.1f} ms")  # noqa: T201
    assert gap < _MAX_STALL_S


async def test_untrusted_marker_rehash_does_not_stall_the_loop(handed_off) -> None:
    """A digest-only marker forces the full re-hash — in a worker thread."""
    store, local, ref = handed_off
    Path(str(local) + ".sha256").write_text(
        Path(str(local) + ".sha256").read_text().split()[0]
    )
    inp = _HandOff(ref=ref)

    async def _handoff() -> None:
        if has_refs_to_materialize(inp):
            assert await materialize_file_refs(store, inp) is inp

    gap = await _max_loop_gap(_handoff)
    print(f"\n[load] re-hashed 2 GiB hand-off: worst loop gap {gap * 1000:.1f} ms")  # noqa: T201
    assert gap < _MAX_STALL_S


async def test_control_on_loop_hash_is_seen_as_a_stall(handed_off) -> None:
    """What the pre-FND-4081 check did; proves the ticker detects a stall."""
    _, local, _ = handed_off

    async def _on_loop() -> None:
        integrity._sha256_file_sync(local)

    gap = await _max_loop_gap(_on_loop)
    print(f"\n[load] control on-loop 2 GiB hash: worst loop gap {gap * 1000:.1f} ms")  # noqa: T201
    assert gap > _MAX_STALL_S
