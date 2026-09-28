"""Worker-side helper that binds a per-invocation :class:`HandlerContext`.

Lives in the SDK, not the api package, because it reads the worker's
infrastructure (the Dapr secret store). The SDR activities and the injected
preflight gate use it so each handler invocation runs with the same
ContextVar-backed context the HTTP path builds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from application_sdk_api.handler.context import HandlerContext, bind_handler_context


@contextmanager
def bind_invocation_context(
    app_name: str, credentials: list[Any]
) -> Iterator[HandlerContext]:
    """Build and bind a per-invocation :class:`HandlerContext` for the block.

    Shared by the SDR activities and the injected preflight gate so each handler
    invocation runs with the same ContextVar-backed context the HTTP path builds
    (app name, credentials, and the worker's secret store when present).
    """
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — lazy: avoid import cycle at module load
        get_infrastructure,
    )

    infra = get_infrastructure()
    secret_store = infra.secret_store if infra is not None else None
    context = HandlerContext(
        app_name=app_name,
        request_id=uuid4(),
        started_at=datetime.now(UTC),
        _credentials=list(credentials),
        _secret_store=secret_store,
    )
    with bind_handler_context(context):
        yield context
