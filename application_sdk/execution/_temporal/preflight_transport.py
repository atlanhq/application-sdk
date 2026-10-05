"""The one seam the preflight gate reaches an app's handler through (FND-3280).

Imports run worker → handler only, and the handler is moving to a shared pod
that serves every app. The gate's two calls into the handler —
``preflight_check`` and ``warmup`` — therefore go
through :class:`PreflightTransport` rather than a ``Handler`` the gate holds
directly, so the in-process call today and an HTTP call to the handler pod
later are interchangeable without touching the gate.

The gate keeps everything that is not the call itself: credential resolution,
the per-invocation ``HandlerContext`` binding, the budgets (the call is waited
on with ``asyncio.wait`` and cancelled at the deadline, never awaited past it),
and ``FailureCategory`` attribution of whatever the call returns or raises. A
transport only carries one input to the handler and one result back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from application_sdk.handler.contracts import (
        PreflightInput,
        PreflightOutput,
        WarmupInput,
        WarmupObservation,
    )

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler


class PreflightTransport(Protocol):
    """How the gate calls an app's preflight handler.

    Every input and output is an existing handler contract —
    :class:`~application_sdk.handler.contracts.PreflightInput` or
    :class:`~application_sdk.handler.contracts.WarmupInput` in,
    :class:`~application_sdk.handler.contracts.PreflightOutput` or
    :class:`~application_sdk.handler.contracts.WarmupObservation` back. These are
    already serialisable wire contracts (the ``/workflows/v1/check`` and
    ``/workflows/v1/warmup`` routes take and return them as JSON), so an HTTP
    transport is a drop-in implementation of this protocol, not a new contract.

    A transport raises what the handler raised (or, remotely, the typed
    ``AppError`` the response carried); the gate attributes it. It must stay
    cancellable at an await point, because the gate cancels an overrunning call
    rather than waiting for it.

    A :class:`~application_sdk.handler.base.Handler` satisfies this protocol
    structurally, so passing one where a transport is expected is the
    in-process call. The worker wraps it in :class:`InProcessPreflightTransport`
    to make that choice explicit.
    """

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run the handler's preflight checks for *input*."""
        ...

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        """Probe the source's compute once, pushing its warmup forward."""
        ...


class InProcessPreflightTransport:
    """The default transport: call a ``Handler`` instance in this process.

    Today's behaviour — the handler runs on the worker's event loop, in the
    gate activity's frame — behind the :class:`PreflightTransport` seam.
    """

    def __init__(self, handler: Handler) -> None:
        self.handler = handler

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return await self.handler.preflight_check(input)

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        return await self.handler.warmup(input)
