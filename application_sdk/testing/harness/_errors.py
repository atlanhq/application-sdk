"""Typed error leaves for the shared test harness.

Private module: leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness`. Mirrors
:mod:`application_sdk.testing.e2e._errors`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import PreconditionError

__all__ = ["SyncBridgeInAsyncContextError"]


@dataclass(kw_only=True)
class SyncBridgeInAsyncContextError(PreconditionError):
    """:func:`~application_sdk.testing.harness.run_sync` was called from a running loop.

    The bridge owns its thread's event loop, so it cannot be re-entered from
    inside one: ``run_until_complete`` on a running loop raises, and standing up
    a second loop on the same thread would fragment every client the harness
    caches per loop. The caller wants the ``_async`` twin of whatever it called.
    """

    code: ClassVar[str] = "PRECONDITION_SYNC_BRIDGE_IN_ASYNC_CONTEXT"
    component: str | None = "harness_sync_bridge"
