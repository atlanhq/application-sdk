"""Request context for Handler execution.

Provides HandlerContext, the execution context passed to handlers during
HTTP request processing. Unlike AppContext (which handles Temporal workflow
concerns), HandlerContext is focused on HTTP request handling.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from application_sdk._context_errors import (  # noqa: F401 — AppContextError re-exported
    AppContextError,
    SecretStoreNotConfiguredError,
)
from application_sdk._install import worker_only_missing
from application_sdk._logging import get_logger
from application_sdk._runtime.offload import offload_owner

if TYPE_CHECKING:
    from application_sdk.handler.contracts import HandlerCredential


class SecretStore(Protocol):
    """What :class:`HandlerContext` needs from a secret store.

    The worker passes its Dapr-backed store. The consolidated API host has no
    secret store and passes none, so ``get_secret`` raises
    :class:`SecretStoreNotConfiguredError` there while ``get_credential`` (the
    request's own credentials) works everywhere.
    """

    async def get(self, name: str) -> str: ...

    async def get_optional(self, name: str) -> str | None: ...


#: Credential directories a handler invocation failed to remove, with their
#: on-removed callbacks. Process-wide, because the invocation's context is
#: discarded on exit: every later invocation's exit retries them, so a
#: transient failure does not leave secrets behind for the process lifetime.
_pending_credential_dirs: dict[str, Callable[[str], None]] = {}
_pending_credential_dirs_lock = threading.Lock()


def _remove_credential_dirs(dirs: dict[str, Callable[[str], None]]) -> None:
    """Remove each directory in *dirs*, then call its ``on_removed`` callback.

    A directory that is still there afterwards goes to the process-wide
    retry queue (logged, path only); one that is gone leaves the queue.
    """
    for path, on_removed in dirs.items():
        shutil.rmtree(path, ignore_errors=True)
        if os.path.lexists(path):
            get_logger(__name__).warning(
                "Could not remove credential directory %s; retrying on the next "
                "handler exit",
                path,
            )
            with _pending_credential_dirs_lock:
                _pending_credential_dirs[path] = on_removed
        else:
            with _pending_credential_dirs_lock:
                _pending_credential_dirs.pop(path, None)
            on_removed(path)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HandlerContext:
    """Execution context passed to Handlers during request processing.

    Provides request identification, credential access, and structured
    logging for HTTP handler invocations.

    Usage:
        async def test_auth(self, input: AuthInput) -> AuthOutput:
            api_key = self.context.get_credential("api_key")
            self.context.log_info("Testing authentication")
    """

    app_name: str
    """The App name this handler serves."""

    request_id: UUID = field(default_factory=uuid4)
    """Unique identifier for this request."""

    started_at: datetime = field(default_factory=_utc_now)
    """When the request started."""

    _credentials: list[HandlerCredential] = field(default_factory=list, repr=False)
    """Credentials extracted from request (omitted from repr for security)."""

    _secret_store: SecretStore | None = field(default=None, repr=False)
    """Secret store injected from InfrastructureContext."""

    _logger: Any = field(default=None, repr=False)
    """Cached bound logger instance."""

    _credential_dirs: dict[str, Callable[[str], None]] = field(
        default_factory=dict, repr=False
    )
    """Private credential directories made during this invocation, each with
    the callback to run once it is removed; removed when
    :func:`bind_handler_context` exits."""

    @property
    def request_id_str(self) -> str:
        """Request ID as string."""
        return str(self.request_id)

    @property
    def credentials(self) -> list[HandlerCredential]:
        """Credentials extracted from the HTTP request."""
        return self._credentials

    def get_credential(self, key: str) -> str | None:
        """Get a specific credential value by key, or None if not found."""
        for cred in self._credentials:
            if cred.key == key:
                return cred.value
        return None

    def has_credential(self, key: str) -> bool:
        """Check if a credential exists in the context."""
        return any(cred.key == key for cred in self._credentials)

    @property
    def log(self) -> Any:
        """Logger with app context (app_name and request_id are auto-injected by the adapter)."""
        if self._logger is None:
            self._logger = get_logger(__name__)
        return self._logger

    async def get_secret(self, name: str) -> str:
        """Get a secret by name from the secret store.

        Args:
            name: Secret name.

        Returns:
            The secret value.

        Raises:
            SecretStoreNotConfiguredError: If no secret store is configured.
        """
        if self._secret_store is None:
            raise SecretStoreNotConfiguredError()
        return await self._secret_store.get(name)

    async def get_secret_optional(self, name: str) -> str | None:
        """Get a secret by name, returning None if not found or not configured.

        Args:
            name: Secret name.

        Returns:
            The secret value, or None if not found or not configured.
        """
        if self._secret_store is None:
            return None
        return await self._secret_store.get_optional(name)

    def log_debug(self, message: str, **kwargs: Any) -> None:
        self.log.debug(message, **kwargs)

    def log_info(self, message: str, **kwargs: Any) -> None:
        self.log.info(message, **kwargs)

    def log_warning(self, message: str, **kwargs: Any) -> None:
        self.log.warning(message, **kwargs)

    def log_error(self, message: str, **kwargs: Any) -> None:
        self.log.error(message, **kwargs)

    def _track_credential_dir(
        self, path: str, on_removed: Callable[[str], None]
    ) -> None:
        """Remove *path* (a private credential directory) when this invocation
        ends, then call ``on_removed(path)``.

        The callback keeps this module free of a credentials import, which
        would pull the credentials package into every handler install.
        """
        self._credential_dirs.setdefault(path, on_removed)

    def _remove_credential_dirs(self) -> None:
        """Remove the credential directories this invocation made, and retry
        any earlier invocation failed to remove."""
        with _pending_credential_dirs_lock:
            dirs = {**_pending_credential_dirs, **self._credential_dirs}
        self._credential_dirs.clear()
        _remove_credential_dirs(dirs)

    def elapsed_ms(self) -> float:
        """Elapsed time since request started in milliseconds."""
        delta = datetime.now(UTC) - self.started_at
        return delta.total_seconds() * 1000


# ---------------------------------------------------------------------------
# ContextVar-backed context binding
# ---------------------------------------------------------------------------

_current_handler_context: ContextVar[HandlerContext | None] = ContextVar(
    "handler_context", default=None
)


def get_handler_context() -> HandlerContext | None:
    """Return the HandlerContext for the current asyncio task, or None."""
    return _current_handler_context.get()


@contextmanager
def bind_handler_context(ctx: HandlerContext) -> Iterator[HandlerContext]:
    """Bind *ctx* as the active handler context for the duration of the block.

    Uses a ContextVar so concurrent coroutines on a shared Handler instance
    (FastAPI requests, Temporal SDR activities) cannot overwrite each other's
    context.  Each asyncio Task gets its own copy of the ContextVar namespace,
    so token-based reset is both safe and strictly scoped to the current task.

    Blocking calls offloaded inside the block are charged to ``ctx.app_name``
    (FND-2973), so on a pod serving several apps each app's share of the
    blocking pool is counted and capped separately.

    Credential directories made inside the block (``resolve_credential_file``,
    ``credential_dir``) are removed when it exits: a handler call is their
    scope, as a run is inside a task, so a long-lived server does not
    accumulate one private directory of secrets per request.
    """
    token = _current_handler_context.set(ctx)
    try:
        with offload_owner(ctx.app_name):
            yield ctx
    finally:
        _current_handler_context.reset(token)
        ctx._remove_credential_dirs()


@contextmanager
def bind_invocation_context(
    app_name: str, credentials: list[Any]
) -> Iterator[HandlerContext]:
    """Build and bind a per-invocation :class:`HandlerContext` for the block.

    Shared by the SDR activities and the injected preflight gate so each handler
    invocation runs with the same ContextVar-backed context the HTTP path builds
    (app name, credentials, and the worker's secret store when present).
    """
    try:
        from application_sdk.infrastructure.context import (  # noqa: PLC0415 — lazy: avoid import cycle; absent on an api-only install
            get_infrastructure,
        )
    except ModuleNotFoundError as exc:
        if not worker_only_missing(exc):
            raise
        infra = None  # api-only install: no worker infrastructure, so no secret store
    else:
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
