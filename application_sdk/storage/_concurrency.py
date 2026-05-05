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
