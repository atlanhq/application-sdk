"""Shared handlers, contracts and client factory for the handler-service tests.

The ``test_service_*.py`` modules all build the service from these; keeping
them here lets each route area live in its own file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from application_sdk.contracts.base import Input, Output
from application_sdk.errors.leaves import AuthError
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
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
from application_sdk.handler.service import (
    create_app_handler_service,
)

# ---------------------------------------------------------------------------
# Test Handler implementation
# ---------------------------------------------------------------------------


class _TestHandler(Handler):
    """Minimal handler for testing — returns SqlMetadataOutput."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _ConfigCapture(_TestHandler):
    """Records the (metadata, connection_config) each preflight_check received."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict[str, dict]] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.received.append(
            {
                "metadata": dict(input.metadata),
                "connection_config": dict(input.connection_config),
            }
        )
        return PreflightOutput(status=PreflightStatus.READY, message="ready")


class _ApiTreeHandler(Handler):
    """Handler that returns ApiMetadataOutput (BI connector path)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return ApiMetadataOutput(
            objects=[
                ApiMetadataObject(value="tag-1", title="Tag One", node_type="tag"),
                ApiMetadataObject(value="tag-2", title="Tag Two", node_type="tag"),
            ]
        )


class _AuthFailedHandler(Handler):
    """Handler that returns AuthStatus.FAILED (no exception)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.FAILED, message="bad credentials")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthTypedFailureHandler(Handler):
    """Handler that returns AuthStatus.FAILED carrying a typed error."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(
            status=AuthStatus.FAILED,
            message="Authentication failed",
            error=AuthError(
                message="The source rejected the credentials.",
                suggested_action="Check the username and password.",
            ),
        )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthCausedFailureHandler(Handler):
    """Handler that builds its typed error from the caught exception (E019 advice)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        try:
            raise RuntimeError("could not connect to host db.internal user=svc_x")
        except RuntimeError as exc:
            return AuthOutput(
                status=AuthStatus.FAILED,
                error=AuthError(
                    message="The source rejected the credentials.", cause=exc
                ),
            )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthExpiredHandler(Handler):
    """Handler that returns AuthStatus.EXPIRED."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.EXPIRED, message="token expired")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthInvalidCredsHandler(Handler):
    """Handler that returns AuthStatus.INVALID_CREDENTIALS."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.INVALID_CREDENTIALS, message="wrong key")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthUnhandledExceptionHandler(Handler):
    """Handler whose test_auth raises an unexpected exception."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise RuntimeError("unexpected db connection error")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _SourceDownHandler(Handler):
    """Handler whose source probes raise the typed SOURCE_UNAVAILABLE leaf."""

    @staticmethod
    def _down() -> Exception:
        from application_sdk.errors.leaves import SourceUnavailableError

        return SourceUnavailableError(message="the warehouse did not answer")

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise self._down()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise self._down()

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise self._down()


class _FailingHandler(Handler):
    """Handler that raises HandlerError."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise HandlerError("auth failed")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise HandlerError("preflight failed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise HandlerError("metadata failed")


# ---------------------------------------------------------------------------
# Module-level contract types for routing tests
# (locally-defined dataclasses fail get_type_hints() when
#  'from __future__ import annotations' is active)
# ---------------------------------------------------------------------------


# Plain Pydantic subclasses — @dataclass would generate a conflicting __init__
# that breaks Pydantic's __pydantic_fields_set__ initialisation.
class _RoutingInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    name: str = ""


class _RoutingOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    result: str = ""


# Contracts for TestGetResultMultiEntrypointOutputType — must be at module
# level because get_type_hints() resolves annotation strings against module
# globals when 'from __future__ import annotations' is active.
class _AlphaInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


class _AlphaOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    alpha_field: str = ""


class _BetaInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


class _BetaOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    beta_field: str = ""


# ---------------------------------------------------------------------------
# Helper: create test client
# ---------------------------------------------------------------------------


def _make_client(handler: Handler | None = None) -> TestClient:
    handler = handler or _TestHandler()
    app = create_app_handler_service(handler, app_name="test-app")
    return TestClient(app)


def _install_fake_app_module(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, leaf: str, **attrs: object
) -> None:
    """Inject ``app.<segment>.<leaf>`` into ``sys.modules`` with ``attrs`` set.

    Shared by the per-entrypoint ``core`` (compute_manifest) and ``handler``
    (test_auth/...) test helpers — simulates the on-disk convention without
    touching the filesystem.
    """
    import sys
    import types

    from application_sdk.app.entrypoint import entrypoint_module_segment

    snake = entrypoint_module_segment(entrypoint)
    # Ensure parent packages exist so ``import app.<snake>.<leaf>`` resolves.
    for parent in ("app", f"app.{snake}"):
        if parent not in sys.modules:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, parent, pkg)
    module = types.ModuleType(f"app.{snake}.{leaf}")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, f"app.{snake}.{leaf}", module)
