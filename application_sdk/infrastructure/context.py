"""Infrastructure context for distributed services.

Provides a ContextVar-based infrastructure context that holds infrastructure
service instances (state store, secret store, storage, events) and propagates
them across activity boundaries.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from application_sdk.infrastructure.bindings import Binding, StorageBinding
    from application_sdk.infrastructure.secrets import SecretStore
    from application_sdk.infrastructure.state import StateStore


@dataclass(frozen=True)
class InfrastructureContext:
    """Holds the current infrastructure services for a process.

    Created once at startup by ``main.py`` and stored in a ContextVar so that
    activities can access it via ``get_infrastructure()``.
    """

    state_store: "StateStore | None" = field(default=None)
    secret_store: "SecretStore | None" = field(default=None)
    storage_binding: "StorageBinding | None" = field(default=None)
    event_binding: "Binding | None" = field(default=None)


_infrastructure_ctx: ContextVar[InfrastructureContext | None] = ContextVar(
    "infrastructure_ctx", default=None
)


def get_infrastructure() -> InfrastructureContext | None:
    """Get the current infrastructure context.

    Returns:
        Current InfrastructureContext, or None if not set.
    """
    return _infrastructure_ctx.get()


def set_infrastructure(ctx: InfrastructureContext) -> None:
    """Set the current infrastructure context.

    Called once at startup (by ``main.py``) before the worker or handler
    service is started.

    Args:
        ctx: The infrastructure context to set.
    """
    _infrastructure_ctx.set(ctx)
