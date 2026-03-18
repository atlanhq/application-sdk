"""Request context for Handler execution.

Provides HandlerContext, the execution context passed to handlers during
HTTP request processing. Unlike AppContext (which handles Temporal workflow
concerns), HandlerContext is focused on HTTP request handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from loguru import logger

if TYPE_CHECKING:
    from application_sdk.handler.contracts import HandlerCredential
    from application_sdk.infrastructure.secrets import SecretStore
    from application_sdk.infrastructure.state import StateStore


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

    _secret_store: "SecretStore | None" = field(default=None, repr=False)
    """Secret store injected from InfrastructureContext."""

    _state_store: "StateStore | None" = field(default=None, repr=False)
    """State store injected from InfrastructureContext."""

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
        """Bound logger with app_name and request_id context."""
        if self._logger is None:
            self._logger = logger.bind(
                app_name=self.app_name,
                request_id=self.request_id_str,
            )
        return self._logger

    async def get_secret(self, name: str) -> str:
        """Get a secret by name from the secret store.

        Args:
            name: Secret name.

        Returns:
            The secret value.

        Raises:
            RuntimeError: If no secret store is configured.
        """
        if self._secret_store is None:
            raise RuntimeError("No secret store configured")
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
