"""Bounded-concurrency fan-out for storage operations.

Every prefix-level operation (``download_prefix``, ``upload_prefix``,
``delete_prefix``'s per-key fallback) fans out through :func:`_run_bounded`, so
all of them share one failure contract:

* The first failure cancels the remaining work and **waits** for it — tasks and
  the threads they offloaded — before propagating. ``asyncio.gather`` did
  neither: siblings kept running after the caller saw the error, and a
  cancelled task's ``run_in_thread`` work kept writing after the caller was
  gone, which a retry of the same operation then raced (a prior incident).
* A failure made only of ``StorageError`` surfaces as the bare ``StorageError``,
  not an ``ExceptionGroup``.

The offloads a primitive makes outside its fan-out go through
:func:`_run_drained`, which gives a single call the same unwind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from typing import Any, TypeVar

from application_sdk._runtime.offload import drain_offloads, tracking_offloads

T = TypeVar("T")


async def _run_bounded(
    coros: Iterable[Coroutine[Any, Any, T]],
    max_concurrency: int | None,
) -> list[T]:
    """Run *coros* concurrently, at most *max_concurrency* at a time.

    ``None`` applies no bound of its own, for callers that already hold one.
    Results are returned in input order.

    Raises:
        StorageError: The first failure, when every failure is a
            ``StorageError``. Remaining coroutines are cancelled, and both
            they and any ``run_in_thread`` work they started have finished
            before this raises.
        BaseExceptionGroup: When a failure is not a ``StorageError``.
            Unwrapping a mixed group would demote every other leaf to
            ``__cause__`` — reachable in a traceback, invisible to an
            ``except`` clause — so something unforeseen is surfaced whole.
        asyncio.CancelledError: If the caller is cancelled, after the same
            drain.
    """
    # Lazy: storage/__init__.py loads sibling modules, and errors imports them.
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageError,
    )

    sem = asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None

    async def _run(coro: Coroutine[Any, Any, T]) -> T:
        if sem is None:
            return await coro
        async with sem:
            return await coro

    # A TaskGroup (vs gather) gives cancel-and-await semantics for the *tasks*:
    # on the first failure, or when the caller is cancelled, it cancels the rest
    # and does not return until they have unwound. That is not enough on its
    # own — a task awaiting run_in_thread unwinds the moment it is cancelled,
    # while its thread runs on — so the offloads are tracked and drained too.
    with tracking_offloads() as pending:
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(_run(c)) for c in coros]
        except BaseExceptionGroup as group:
            await drain_offloads(pending)
            if all(isinstance(leaf, StorageError) for leaf in group.exceptions):
                raise group.exceptions[0] from group
            raise
        except BaseException:
            await drain_offloads(pending)
            raise
    return [t.result() for t in tasks]


async def _gather_with_semaphore(
    coros: Iterable[Coroutine[Any, Any, T]],
    sem: asyncio.Semaphore,
) -> list[T]:
    """Run coroutines concurrently, at most ``sem`` slots at a time.

    Preserves input order in the returned list. The failure contract is
    :func:`_run_bounded`'s: the first failure cancels and drains the rest.
    """

    async def _run(coro: Coroutine[Any, Any, T]) -> T:
        async with sem:
            return await coro

    return await _run_bounded([_run(c) for c in coros], None)


async def _run_drained(coro: Coroutine[Any, Any, T]) -> T:
    """Await *coro*; on failure or cancellation, drain what it offloaded first.

    The single-call counterpart of :func:`_run_bounded`, for the offloads a
    primitive makes outside its fan-out (a sync's index write and prune). A
    cancelled ``run_in_thread`` stops waiting while its thread runs on; this
    waits for that thread, on the same no-progress terms as the fan-out, before
    the failure propagates. The failure itself is re-raised unchanged -- no
    group wrapping, since there is only one coroutine.
    """
    with tracking_offloads() as pending:
        try:
            return await coro
        except BaseException:
            await drain_offloads(pending)
            raise
