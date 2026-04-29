"""Infrastructure context for distributed services.

Provides a module-level singleton infrastructure context that holds infrastructure
service instances (state store, secret store, storage, events) for use across the
process lifetime.

The context is set once at startup by ``main.py`` and is readable from any code path
including uvicorn HTTP request handlers (which run in isolated ``contextvars.Context``
instances and cannot see ``ContextVar`` values set in the parent task). A
``ContextVar``-based substrate would silently return ``None`` inside FastAPI route
handlers — that mismatch is why this module uses a plain module-level variable
instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.infrastructure.bindings import Binding
    from application_sdk.infrastructure.secrets import SecretStore
    from application_sdk.infrastructure.state import StateStore


@dataclass(frozen=True)
class InfrastructureContext:
    """Holds the current infrastructure services for a process.

    Created once at startup by ``main.py`` and stored in a module-level singleton
    so it is reachable from any code path — including uvicorn HTTP request handlers,
    which run in fresh isolated contexts and cannot inherit ``ContextVar`` values.

    The optional ``_dapr_client`` field holds the shared ``AsyncDaprClient``
    so it can be closed on shutdown via :func:`close_infrastructure`.
    """

    state_store: StateStore | None = field(default=None)
    secret_store: SecretStore | None = field(default=None)
    storage: ObjectStore | None = field(default=None)
    event_binding: Binding | None = field(default=None)
    _dapr_client: Any = field(default=None, repr=False)


_infrastructure: InfrastructureContext | None = None


def get_infrastructure() -> InfrastructureContext | None:
    """Get the current infrastructure context.

    Returns:
        Current InfrastructureContext, or None if not set.
    """
    return _infrastructure


def set_infrastructure(ctx: InfrastructureContext) -> None:
    """Set the current infrastructure context.

    Called once at startup (by ``main.py``) before the worker or handler
    service is started.

    Args:
        ctx: The infrastructure context to set.
    """
    global _infrastructure
    _infrastructure = ctx


def clear_infrastructure() -> None:
    """Clear the current infrastructure context.

    Intended for tests and explicit teardown. Sets the module-level singleton
    to ``None`` so subsequent calls to ``get_infrastructure()`` return ``None``.
    """
    global _infrastructure
    _infrastructure = None


async def close_infrastructure() -> None:
    """Close the shared Dapr client held by the current infrastructure context.

    Safe to call multiple times or when no context is set. Clears the
    module-level singleton after closing so repeated shutdown calls are no-ops.
    Should be called during graceful shutdown to avoid httpx connection pool leaks.
    """
    global _infrastructure
    ctx = _infrastructure
    if ctx and ctx._dapr_client is not None:
        await ctx._dapr_client.close()
    _infrastructure = None
