"""Unit tests for the injected preflight-gate activity (``preflight:gate``).

Separate from the SDR activity tests — the gate is its own module/concern.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import AppContextError
from application_sdk.credentials.ref import CredentialResolvable
from application_sdk.execution._temporal.preflight_gate import (
    build_preflight_gate_activity,
    preflight_gate_activity_name,
)
from application_sdk.handler.base import DefaultHandler, Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    PreflightGateInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)


class _StubHandler(Handler):
    """Records the preflight input it was called with."""

    def __init__(self) -> None:
        super().__init__()
        self.preflight_input: PreflightInput | None = None

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_input = input
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


def _gate(handler: Handler):
    activity = build_preflight_gate_activity(handler, app_name="myapp")
    assert getattr(activity, "__temporal_activity_definition").name == "myapp:preflight"
    return activity


class TestPreflightGateActivity:
    def test_activity_name_is_app_namespaced(self) -> None:
        # Reads as a native workflow step ({app}:preflight), like the app's own
        # {app}:<task> activities — not a foreign sdr:/preflight: namespace.
        assert preflight_gate_activity_name("mysql") == "mysql:preflight"
        assert not preflight_gate_activity_name("mysql").startswith("sdr:")

    def test_gate_input_satisfies_credential_resolvable(self) -> None:
        assert isinstance(PreflightGateInput(), CredentialResolvable)

    async def test_gate_with_default_handler_returns_canonical_success(self) -> None:
        gate = _gate(DefaultHandler())
        result = await gate(PreflightGateInput())
        assert result.canonical_status() is PreflightStatus.SUCCESS

    async def test_gate_resolves_guid_and_calls_handler_with_flattened_creds(
        self,
    ) -> None:
        handler = _StubHandler()
        gate = _gate(handler)

        resolver = mock.MagicMock()
        resolver.resolve_raw = mock.AsyncMock(
            return_value={"host": "db", "username": "u", "extra": {"role": "r"}}
        )
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")

        with (
            mock.patch(
                "application_sdk.execution._temporal.preflight_gate.get_infrastructure",
                return_value=fake_infra,
            ),
            mock.patch(
                "application_sdk.execution._temporal.preflight_gate.CredentialResolver",
                return_value=resolver,
            ),
        ):
            result = await gate(
                PreflightGateInput(credential_guid="guid-123", entrypoint="crawl")
            )

        assert result.status is PreflightStatus.READY
        assert handler.preflight_input is not None
        seen = {c.key: c.value for c in handler.preflight_input.credentials}
        assert seen == {"host": "db", "username": "u", "extra.role": "r"}
        assert handler.preflight_input.entrypoint == "crawl"
        resolver.resolve_raw.assert_awaited_once()

    async def test_gate_without_routing_skips_resolution(self) -> None:
        handler = _StubHandler()
        gate = _gate(handler)
        with mock.patch(
            "application_sdk.execution._temporal.preflight_gate.CredentialResolver",
        ) as resolver_cls:
            await gate(PreflightGateInput())
        resolver_cls.assert_not_called()
        assert handler.preflight_input is not None
        assert handler.preflight_input.credentials == []

    async def test_gate_clears_context_after_call(self) -> None:
        handler = _StubHandler()
        gate = _gate(handler)
        await gate(PreflightGateInput())
        with pytest.raises(AppContextError):
            _ = handler.context
