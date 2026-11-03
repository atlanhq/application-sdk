"""Handler ABC and default implementations.

Provides the Handler abstract base class that apps subclass to implement
authentication, preflight, and metadata operations for their HTTP service.

DefaultHandler provides pass-through implementations that always succeed,
useful for apps that don't need custom handler logic.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from application_sdk._context_errors import AppContextError
from application_sdk.errors import HANDLER_ERROR, ErrorCode
from application_sdk.errors.base import AppError
from application_sdk.handler.context import get_handler_context
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    ManifestInput,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
    WarmupInput,
    WarmupObservation,
    WarmupState,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from application_sdk.handler.context import HandlerContext


class HandlerError(AppError):
    """Deprecated: use a typed ``AppError`` subclass — removed in v4.0.

    Category varies by raise site; defaults to INTERNAL until callers are
    migrated to typed errors (Phase 5 triage).
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = HANDLER_ERROR
    code: ClassVar[str] = "HANDLER"

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
        warnings.warn(
            "HandlerError is deprecated; use a typed application_sdk.errors.AppError subclass "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        AppError.__init__(self, message=message, cause=cause, app_name=app_name or None)
        self._legacy_error_code = error_code
        self.http_status = http_status
        self.handler_name = handler_name

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._legacy_error_code
            if self._legacy_error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
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

    @property
    def context(self) -> HandlerContext:
        """Current request context.

        Raises:
            RuntimeError: If accessed outside of a handler method invocation.
        """
        ctx = get_handler_context()
        if ctx is None:
            raise AppContextError(
                "Handler context is not set. "
                "Access self.context only inside a handler method "
                "(test_auth, preflight_check, fetch_metadata, warmup, "
                "compute_manifest)."
            )
        return ctx

    def routers(self) -> Sequence[APIRouter]:
        """FastAPI routers with this app's own endpoints (none by default).

        Override to serve endpoints beyond auth / check / metadata::

            router = APIRouter(prefix="/workflows/v1/metadata")

            @router.post("/projects")
            async def projects(body: ProjectsRequest) -> JSONResponse: ...

            class MyHandler(Handler):
                def routers(self) -> Sequence[APIRouter]:
                    return [router]

        The worker's handler service and the consolidated API host both serve
        them, after the SDK's routes; redefining an SDK path is refused at
        startup. Keep router code in the handler's own files (imported
        relatively) so it ships to the host with the handler.
        """
        return ()

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

        One method, two surfaces: the HTTP ``/check`` endpoint (Sage UI) and the
        injected pre-extraction gate. To abort a run, return
        ``status=PreflightStatus.NOT_READY``; ``READY``/``PARTIAL`` proceed.
        Express a readiness verdict through the returned status — that is the
        contract, and it is what both surfaces render.

        Raising is not equivalent, and what it means depends on the error's type:

        - A retryable ``SourceUnavailableError`` (the source did not answer) or
          ``RateLimitedError`` (it answered 429) means "I could not ask this
          time". A hard gate retries it and blocks only if it persists to the
          final attempt, under its own category; soft mode reports and proceeds.
          Compute that is cold, warming or queued is not this: report it from
          :meth:`warmup`.
        - A ``DependencyUnavailableError``, ``ResourceExhaustedError`` or
          ``CancelledError`` is the deprecated fail-open idiom: the gate fails
          open on it in both postures, with a ``DeprecationWarning``.
        - Anything else — an untyped crash, another typed source error, or
          overrunning ``input.timeout_seconds`` — is treated as an unverifiable
          source and is subject to the app's ``preflight_gate_mode``. In hard
          mode it aborts the run when its category is one the customer can act
          on (``TIMEOUT`` and an untyped crash's ``INTERNAL`` never do).

        Keep probes awaitable: the gate cancels this method at the budget, and
        cancellation only lands at an ``await``. Blocking synchronous I/O on the
        event loop escapes the budget and stalls the worker's other activities.

        Args:
            input: Credentials, connection config, and checks to run. On the gate
                path ``connection_config`` and ``metadata`` are derived from the
                extraction input's own fields, and ``timeout_seconds`` is the
                remaining enforced budget.

        Returns:
            PreflightOutput whose ``status`` decides the gate.
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

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        """Probe the source's compute, pushing its warmup forward.

        Optional. The default reports ``READY``, so an app that does not
        override it passes on the first probe and behaves exactly as it did
        before warmup existed.

        Idempotent and stateless: each call both pushes the warmup forward and
        reports where it is, and the SDK holds no warmup state between calls.
        The gate calls it once per poll on durable timers, and
        ``POST /workflows/v1/warmup`` once per request. A query-probe app
        submits its probe query (which wakes a suspended warehouse), waits up to
        ``input.probe_timeout_seconds``, and reports ``READY`` if it answered,
        ``WARMING`` / ``QUEUED`` if not. Whether to cancel a probe still pending
        at the timeout is the app's call; each poll submits again, so the
        default guidance is to cancel.

        Raise a typed AUTH / PERMISSION / NOT_FOUND ``AppError`` for a failure
        no amount of waiting fixes; return ``UNAVAILABLE`` for a source that
        will not get ready on its own. Never use privileges beyond what the
        app's checks already verify.

        Args:
            input: Credentials and connection config, as sent to ``/check``,
                plus the probe timeout.

        Returns:
            What this probe saw.
        """
        return WarmupObservation(state=WarmupState.READY)

    async def compute_manifest(self, input: ManifestInput) -> dict[str, Any] | None:
        """The manifest to serve for this request, when it cannot be static.

        Optional. The default returns ``None``, which serves the generated
        manifest unchanged. Override it for a manifest that depends on the
        request's ``fe_inputs`` or on the tenant; return ``None`` for an entry
        point whose generated manifest is right as it is.

        The worker's handler service and the consolidated API host both call
        it for a request that names an entry point. Task-queue templates in the
        result (``atlan-<app>-{deployment_name}``) are stamped with the queue
        the app's worker polls, as in a generated manifest: build them from
        ``self.context.app_name``. A request carries no credentials here. On
        the worker an ``app.<segment>.core.compute_manifest`` hook, where one
        exists, wins; the host never runs one.

        Args:
            input: The entry point, its generated manifest and the form values.

        Returns:
            The manifest to serve, or ``None`` for the generated one.
        """
        return None


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
        """Returns READY (no checks) when no handler is registered."""
        return PreflightOutput(
            status=PreflightStatus.READY, message="No preflight handler registered"
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Always returns empty metadata."""
        return SqlMetadataOutput(objects=[])
