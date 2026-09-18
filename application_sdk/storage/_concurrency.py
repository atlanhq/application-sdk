"""Bounded-concurrency gather helper for storage operations."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from typing import Any, TypeVar

T = TypeVar("T")


async def _gather_with_semaphore(
    coros: Iterable[Coroutine[Any, Any, T]],
    sem: asyncio.Semaphore,
) -> list[T]:
    """Run coroutines concurrently, at most ``sem`` slots at a time.

    Preserves input order in the returned list.  Raises immediately on the
    first exception (remaining tasks are cancelled by ``asyncio.gather``
    default behaviour).
    """

    async def _run(coro: Coroutine[Any, Any, T]) -> T:
        async with sem:
            return await coro

    return list(await asyncio.gather(*[_run(c) for c in coros]))


async def _gather_size_tiered(
    sized_coros: Iterable[tuple[int, Coroutine[Any, Any, T]]],
    *,
    small_threshold: int,
    small_limit: int,
    large_limit: int,
    return_exceptions: bool = False,
) -> list[T | BaseException]:
    """Run ``(size, coroutine)`` pairs concurrently with a per-size-tier bound.

    A coroutine whose ``size`` is at or below *small_threshold* runs under a
    semaphore of *small_limit* slots; anything larger runs under *large_limit*.
    The two tiers exist because a transfer's cost has two different shapes: a
    small object is buffered whole and costs a fixed number of round trips
    regardless of its bytes, so a directory of thousands of them is
    round-trip-bound and only a wide fan-out helps; a large object carries
    multipart buffers (part size times part concurrency) for as long as it is
    in flight, so its tier stays narrow to keep peak memory bounded. With
    *small_threshold* ``<= 0`` every coroutine takes the large tier. (FND-1339)

    Preserves input order in the returned list. With *return_exceptions*
    ``False`` the first exception propagates (``asyncio.gather`` semantics);
    with ``True`` each failure is returned in place so the caller decides.
    """
    small_sem = asyncio.Semaphore(max(1, small_limit))
    large_sem = asyncio.Semaphore(max(1, large_limit))

    async def _run(size: int, coro: Coroutine[Any, Any, T]) -> T:
        sem = (
            small_sem if 0 < small_threshold and size <= small_threshold else large_sem
        )
        async with sem:
            return await coro

    # conformance: ignore[E010] a bounded-gather primitive: the caller owns the results and checks them (every call site filters BaseException or lets the first propagate)
    return list(
        await asyncio.gather(
            *[_run(size, coro) for size, coro in sized_coros],
            return_exceptions=return_exceptions,
        )
    )
