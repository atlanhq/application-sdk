"""Process-wide worker shutdown flag.

Set by the SIGINT/SIGTERM handler installed in ``application_sdk.main``;
read by the activity wrapper to recognise that an in-flight
``asyncio.CancelledError`` is being raised because the worker pod is
terminating, not because workflow code requested cancellation.

The activity wrapper consults ``is_worker_shutting_down()`` inside its
exception handler and, if true, raises
``ApplicationError(type=WORKER_EVICTED_TYPE, non_retryable=True)`` so the
SDK's per-activity eviction loop can re-dispatch the activity on a fresh
worker without burning the application-error retry budget.

Single-process state: a worker pod runs one Python process, so a module-level
flag is the right scope. Backed by ``threading.Event`` because:
    - ``set()`` and ``is_set()`` are inherently atomic, so there is no need
      for an explicit lock around the read on the activity hot path.
    - ``set()`` is callable from a signal handler thread without coordination
      with the asyncio event loop.
    - Tests can ``clear()`` between cases via ``reset_worker_shutting_down``.
"""

from __future__ import annotations

import threading

_shutdown_event = threading.Event()


def is_worker_shutting_down() -> bool:
    """Return True iff the worker has begun graceful shutdown.

    Read by the activity wrapper to attribute ``asyncio.CancelledError`` to
    pod termination rather than ordinary workflow-driven cancellation.
    """
    return _shutdown_event.is_set()


def mark_worker_shutting_down() -> None:
    """Mark the worker as shutting down. Called from the signal handler.

    Idempotent and thread-safe by virtue of ``threading.Event``.
    """
    _shutdown_event.set()


def reset_worker_shutting_down() -> None:
    """Reset the flag. Test-only; not part of the public surface."""
    _shutdown_event.clear()
