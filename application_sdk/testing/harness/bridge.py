"""The harness's one sync/async bridge.

Everything below the pytest boundary is ``async`` (decision D1 on FND-224):
:class:`~pyatlan.client.aio.AsyncAtlanClient` rather than the sync
``AtlanClient``, :class:`httpx.AsyncClient` rather than ``urllib.request``,
:func:`asyncio.sleep` rather than :func:`time.sleep`. Exactly one place in the
harness turns a coroutine back into a blocking call, and this is it.

**Why a bridge exists at all.** ``setup_method`` / ``teardown_method`` are
pytest xunit hooks: pytest calls them, and it never awaits them. A sync
composer — a plain ``def`` test, or a helper called from one — is the same
shape. So the harness publishes three entry shapes over one async core:

* the xunit shims on ``BaseE2ETest``, each a one-liner over its ``_async`` twin,
* async fixtures for composers that already run under ``asyncio_mode = auto``,
* this bridge, for a sync composer that has no loop of its own.

**Why it is public.** A third consumer shape asked for it (FND-244): the
runtime scenario suite drives harness reads from sync scenario code. Publishing
it here, rather than in ``application_sdk.common``, is deliberate — a sync
bridge in the core SDK reads as the SDK sanctioning sync bridges in *app* code,
against the async-only stance conformance rules P024 and P031 encode. This one
is test-harness-only, and its import path says so.

**One loop per thread, reused.** The loop is created lazily on first use and
kept for the life of the thread. Standing up a fresh loop per call is what the
five ad-hoc ``asyncio.run`` sites in ``testing/e2e/client.py`` do today, and it
is the bug D1 found: a new loop plus a new ``AsyncAtlanClient`` plus a new TLS
handshake on *every* poll iteration — up to ~50 handshakes for one boolean. The
runtime suite drives this in a loop against a real tenant, so per-call loop
creation is not an abstract cost.

Threads get their own loop rather than sharing one because an event loop is not
thread-safe, and pytest plugins (``pytest-xdist``, ``pytest-timeout``) do run
harness code off the main thread.

**Teardown is the caller's.** Call :func:`close_loop` from the fixture or
process teardown that owns the thread. Not calling it leaks one loop per thread
until the process exits, which is survivable in a test process and is why this
is not a hard requirement — but a long-lived driver that spawns threads should
close them.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness._errors import SyncBridgeInAsyncContextError

logger = get_logger(__name__)

__all__ = ["close_loop", "run_sync"]

T = TypeVar("T")

# One slot per thread. A module-level dict keyed by thread ident would leak
# entries for dead threads; threading.local is collected with the thread.
_state = threading.local()


def _loop_for_this_thread() -> asyncio.AbstractEventLoop:
    """Return this thread's bridge loop, creating it on first use.

    A loop that was closed out from under us (a caller that called
    :func:`close_loop` and then :func:`run_sync` again) is replaced rather than
    reused, so the second call works instead of raising a confusing
    ``RuntimeError: Event loop is closed`` from deep inside asyncio.
    """
    loop: asyncio.AbstractEventLoop | None = getattr(_state, "loop", None)
    if loop is not None and not loop.is_closed():
        return loop
    loop = asyncio.new_event_loop()
    _state.loop = loop
    logger.debug(
        "Harness sync bridge created an event loop for thread %s",
        threading.current_thread().name,
    )
    return loop


def _loop_is_running() -> bool:
    """Whether this thread already has a running event loop.

    :func:`asyncio.get_running_loop` is the only way to ask, and it answers "no"
    by raising, so the ``except`` here is the negative answer rather than a
    swallowed failure — there is nothing to log and nothing to recover from.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # conformance: ignore[E007] the RuntimeError IS the answer "no loop is running" — asyncio offers no non-raising query, so there is no error here to log
        return False
    return True


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* to completion on this thread's bridge loop and return its result.

    Args:
        coro: The coroutine to drive. Typically a call to an ``_async`` twin.

    Returns:
        Whatever the coroutine returns.

    Raises:
        SyncBridgeInAsyncContextError: A loop is already running on this thread.
            Await the ``_async`` twin instead — the message names it.
        BaseException: Anything the coroutine raises propagates unchanged. The
            bridge adds no wrapping, so a connector's ``except`` clauses keep
            matching the leaves they matched before.

    Example:
        .. code-block:: python

            async def run_full_dag_async(self) -> FullDAGOutcome: ...   # the real one

            def run_full_dag(self) -> FullDAGOutcome:                   # one line
                return run_sync(self.run_full_dag_async())
    """
    if _loop_is_running():
        # Close the coroutine explicitly: a never-awaited coroutine that is
        # merely garbage-collected emits a "coroutine was never awaited"
        # RuntimeWarning, which lands in the traceback next to the real error
        # and reads as a second, unrelated bug.
        coro.close()
        raise SyncBridgeInAsyncContextError(
            message=(
                "run_sync() was called from inside a running event loop. "
                "Await the coroutine directly, or call the _async twin of the "
                "method you called (e.g. run_full_dag_async() instead of "
                "run_full_dag())."
            ),
        )
    return _loop_for_this_thread().run_until_complete(coro)


def close_loop() -> None:
    """Close this thread's bridge loop, if it has one. Idempotent.

    Call from the fixture or process teardown that owns the thread. A later
    :func:`run_sync` on the same thread creates a fresh loop rather than
    failing, so closing early is safe.
    """
    loop: asyncio.AbstractEventLoop | None = getattr(_state, "loop", None)
    _state.loop = None
    if loop is None or loop.is_closed():
        return
    try:
        loop.close()
    finally:
        logger.debug(
            "Harness sync bridge closed the event loop for thread %s",
            threading.current_thread().name,
        )
