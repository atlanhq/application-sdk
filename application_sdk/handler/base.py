"""Handler ABC and default implementations.

Provides the Handler abstract base class that apps subclass to implement
authentication, preflight, and metadata operations for their HTTP service.

DefaultHandler provides pass-through implementations that always succeed,
useful for apps that don't need custom handler logic.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from application_sdk.errors import HANDLER_ERROR, ErrorCode
from application_sdk.errors.base import AppError
from application_sdk.handler.context import get_handler_context
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)

if TYPE_CHECKING:
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
            from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports handler.context transitively
                AppContextError,
            )

            raise AppContextError(
                "Handler context is not set. "
                "Access self.context only inside test_auth, preflight_check, or fetch_metadata."
            )
        return ctx

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Warn a subclass that still implements ``test_auth`` separately.

        Authentication is preflight's cheapest layer, not a second operation. Two
        methods meant two implementations that could disagree — credentials that
        pass the UI's "Test authentication" button and then fail the run, or the
        reverse — and two sets of error handling to keep in step.

        Overriding still works and still takes precedence, so nothing breaks today.
        The migration is to drop the override and tag the credential check
        ``depth=CheckDepth.AUTH`` in ``preflight_check`` instead; the default
        :meth:`test_auth` then answers from that one implementation.
        """
        super().__init_subclass__(**kwargs)
        # Only the app's own override is interesting: an intermediate SDK class that
        # already carries the default would otherwise warn every app beneath it.
        if (
            "test_auth" in cls.__dict__
            and cls.__module__.split(".")[0] != "application_sdk"
        ):
            warnings.warn(
                f"{cls.__name__} implements test_auth separately from preflight_check; "
                "use preflight_check with a check tagged depth=CheckDepth.AUTH instead "
                "and drop the test_auth override. Authentication is an AUTH-depth "
                "preflight check, so two implementations can disagree about the same "
                "credential — Handler.test_auth will be removed in v4.0.",
                DeprecationWarning,
                stacklevel=2,
            )

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Test authentication — by default, an ``AUTH``-depth preflight run.

        Not abstract, and not meant to be overridden: it delegates to
        :meth:`preflight_check` with
        ``depth=CheckDepth.AUTH`` and projects the verdict onto the auth contract,
        so a connector implements credential checking exactly once. A run capped at
        ``AUTH`` keeps only the checks tagged at that depth (plus untagged ones, for
        handlers that predate depth tagging), so a permission gap elsewhere does not
        report as bad credentials.

        Overriding is still honoured — and warns, naming v4.0 as the removal.

        Args:
            input: Credentials and connection context.

        Returns:
            AuthOutput with status and optional identity/scope information.
        """
        output = await self.preflight_check(input.to_preflight_input())
        return AuthOutput.from_preflight_output(output)

    @abstractmethod
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run preflight checks (connectivity, permissions, etc.).

        One method, two surfaces: the HTTP ``/check`` endpoint (Sage UI) and the
        injected pre-extraction gate. To abort a run, return
        ``status=PreflightStatus.NOT_READY``; ``READY``/``PARTIAL`` proceed.
        Express a readiness verdict through the returned status — that is the
        contract, and it is what both surfaces render.

        Raising is not equivalent, and what it means depends on the error's type:

        - A **typed plumbing** error (``RateLimitedError``,
          ``DependencyUnavailableError``, ``ResourceExhaustedError``) means "I could
          not determine readiness". The gate fails open on it in both postures.
          This is the right way to report a transient — returning ``NOT_READY``
          for a 429 makes a hard-mode gate fail *closed* on a blip.
        - Anything else — an untyped crash, a typed source error, or overrunning
          ``input.timeout_seconds`` — is treated as an unverifiable source and is
          subject to the app's ``preflight_gate_mode``. In hard mode it aborts the
          run, attributed to preflight.

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


def implements_test_auth(handler: Handler) -> bool:
    """Whether ``handler`` carries its own ``test_auth`` rather than the default.

    The auth ingresses need to know: a legacy override must still be called
    directly (its behaviour is what that app's users see today), while a handler on
    the default can go through the shared check core and pick up credential
    resolution, the enforced budget, classification and the outcome row.

    Walks the MRO up to :class:`Handler` so an app's own intermediate base class
    counts, while the SDK's own classes do not.
    """
    for klass in type(handler).__mro__:
        if klass is Handler:
            return False
        if "test_auth" in klass.__dict__:
            return klass.__module__.split(".")[0] != "application_sdk"
    return False


class DefaultHandler(Handler):
    """Pass-through handler that always returns SUCCESS/READY/empty.

    Useful as a base class for apps that only need to override some operations,
    or as a placeholder during development.
    """

    # test_auth is deliberately NOT overridden: the inherited default answers from
    # this class's own preflight_check, which is the behaviour every app should
    # inherit too. It still reports SUCCESS, since that preflight returns READY.

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Returns READY (no checks) when no handler is registered."""
        return PreflightOutput(
            status=PreflightStatus.READY, message="No preflight handler registered"
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Always returns empty metadata."""
        return SqlMetadataOutput(objects=[])
