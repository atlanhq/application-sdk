"""Handler ABC and default implementations.

Provides the Handler abstract base class that apps subclass to implement
authentication, preflight, and metadata operations for their HTTP service.

DefaultHandler provides pass-through implementations that always succeed,
useful for apps that don't need custom handler logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from application_sdk.errors import HANDLER_ERROR, ErrorCode
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)

if TYPE_CHECKING:
    from application_sdk.handler.context import HandlerContext


class HandlerError(Exception):
    """Structured error raised by handler implementations.

    Carries structured context (error_code, http_status, handler_name, app_name)
    for better observability and consistent HTTP error responses.

    Args:
        message: Human-readable error description.
        error_code: Structured ErrorCode (defaults to HANDLER_ERROR / AAF-HDL-001).
        http_status: HTTP status code for the response (defaults to 500).
        handler_name: Name of the handler that raised the error.
        app_name: Application name for log context.
        cause: Original exception, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode | None = None,
        http_status: int = 500,
        handler_name: str = "",
        app_name: str = "",
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code or HANDLER_ERROR
        self.http_status = http_status
        self.handler_name = handler_name
        self.app_name = app_name
        self.cause = cause

    def __str__(self) -> str:
        parts = [f"[{self.error_code}] {super().__str__()}"]
        if self.handler_name:
            parts.append(f"handler={self.handler_name}")
        if self.app_name:
            parts.append(f"app={self.app_name}")
        return " | ".join(parts)


class Handler(ABC):
    """Abstract base class for per-app handler implementations.

    Subclass Handler to implement the three core operations for your app's
    HTTP service: authentication testing, preflight checks, and metadata
    discovery.

    The handler context (`self.context`) is set by the service layer
    before each method invocation and cleared after. Accessing it outside
    of a handler method raises RuntimeError.

    Example::

        class MyAppHandler(Handler):
            async def test_auth(self, input: AuthInput) -> AuthOutput:
                client = build_client(input.credentials)
                if await client.ping():
                    return AuthOutput(status=AuthStatus.SUCCESS)
                return AuthOutput(status=AuthStatus.FAILED, message="Connection refused")

            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return PreflightOutput(status=PreflightStatus.READY)

            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return SqlMetadataOutput(objects=[])
    """

    _context: HandlerContext | None = None

    @property
    def context(self) -> HandlerContext:
        """Current request context.

        Raises:
            RuntimeError: If accessed outside of a handler method invocation.
        """
        if self._context is None:
            from application_sdk.app.base import AppContextError

            raise AppContextError(
                "Handler context is not set. "
                "Access self.context only inside test_auth, preflight_check, or fetch_metadata."
            )
        return self._context

    @abstractmethod
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Test authentication with the provided credentials.

        Args:
            input: Credentials and connection context.

        Returns:
            AuthOutput with status and optional identity/scope information.

        Raises:
            HandlerError: On authentication errors that should surface as HTTP 500.
        """
        ...

    @abstractmethod
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run preflight checks (connectivity, permissions, etc.).

        Args:
            input: Credentials, connection config, and checks to run.

        Returns:
            PreflightOutput with per-check results and overall status.

        Raises:
            HandlerError: On check errors that should surface as HTTP 500.
        """
        ...

    @abstractmethod
    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Fetch metadata objects from the target system.

        Args:
            input: Credentials, connection config, and filter options.

        Returns:
            A ``SqlMetadataOutput`` (for sqltree widget) or
            ``ApiMetadataOutput`` (for apitree widget).  Both are
            subtypes of ``MetadataOutput``.

        Raises:
            HandlerError: On fetch errors that should surface as HTTP 500.
        """
        ...


class DefaultHandler(Handler):
    """Pass-through handler that always returns SUCCESS/READY/empty.

    Useful as a base class for apps that only need to override some operations,
    or as a placeholder during development.
    """

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Always returns SUCCESS."""
        return AuthOutput(
            status=AuthStatus.SUCCESS, message="Authentication successful"
        )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Always returns READY with no checks."""
        return PreflightOutput(
            status=PreflightStatus.READY,
            message="All preflight checks passed",
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Always returns empty metadata."""
        return SqlMetadataOutput(objects=[])
