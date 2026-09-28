"""Request context for Handler execution.

Provides HandlerContext, the execution context passed to handlers during
HTTP request processing. Unlike AppContext (which handles Temporal workflow
concerns), HandlerContext is focused on HTTP request handling.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import dataclass as _dataclass
from dataclasses import field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol
from uuid import UUID, uuid4

from application_sdk_api.errors import APP_CONTEXT_ERROR, ErrorCode
from application_sdk_api.errors.leaves import InternalError, PreconditionError
from application_sdk_api.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk_api.handler.contracts import HandlerCredential


class SecretStore(Protocol):
    """What :class:`HandlerContext` needs from a secret store.

    The worker passes its Dapr-backed store. The consolidated API host has no
    secret store and passes none, so ``get_secret`` raises
    :class:`SecretStoreNotConfiguredError` there while ``get_credential`` (the
    request's own credentials) works everywhere.
    """

    async def get(self, name: str) -> str: ...

    async def get_optional(self, name: str) -> str | None: ...


@_dataclass(kw_only=True)
class SecretStoreNotConfiguredError(PreconditionError):
    """Secret store required by get_secret / resolve_credential but not configured."""

    code: ClassVar[str] = "PRECONDITION_SECRET_STORE_NOT_CONFIGURED"
    message: str = "No secret store configured"
    resource: str | None = "secret_store"
    expected_state: str | None = "configured"


class AppContextError(InternalError):
    """Raised when App or task context is accessed outside of valid execution scope.

    This is a programming error — it indicates that context-dependent methods
    (e.g. ``self.context``, ``self.heartbeat()``) were called outside of a
    workflow run or @task execution.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_CONTEXT_ERROR
    code: ClassVar[str] = "INTERNAL_APP_CONTEXT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        InternalError.__init__(self, message=message)
        self._legacy_error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._legacy_error_code
            if self._legacy_error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        return f"[{self.error_code.code}] {self.message}"


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
    """
    token = _current_handler_context.set(ctx)
    try:
        yield ctx
    finally:
        _current_handler_context.reset(token)
