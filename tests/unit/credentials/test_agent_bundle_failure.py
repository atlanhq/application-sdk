"""Regression coverage for clean agent bundle lookup failures."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure.secrets import SecretNotFoundError
from application_sdk.testing.mocks import MockSecretStore


@pytest.mark.asyncio
async def test_missing_bundle_suppresses_transport_cause(monkeypatch) -> None:
    """A missing bundle surfaces the credential error, not a sidecar URL."""
    secret_path = "example/agent/bundle"
    request = httpx.Request(
        "GET", "http://localhost:3500/v1.0/secrets/secretstore/example%2Fagent%2Fbundle"
    )
    response = httpx.Response(500, request=request)
    try:
        try:
            raise httpx.HTTPStatusError(
                "Server error 500; password=synthetic-sensitive-value",
                request=request,
                response=response,
            )
        except httpx.HTTPStatusError as exc:
            raise SecretNotFoundError(secret_path) from exc
    except SecretNotFoundError as exc:
        store_error = exc

    store = MockSecretStore()
    get = AsyncMock(side_effect=store_error)
    monkeypatch.setattr(store, "get", get)
    warning = Mock()
    monkeypatch.setattr("application_sdk.credentials.agent.logger.warning", warning)
    spec = AgentCredentialSpec.model_validate(
        {
            "agent-name": "example-agent",
            "key-type": "multi-key",
            "secret-path": secret_path,
            "basic.password": "password",
        }
    )

    with pytest.raises(CredentialNotFoundError) as caught:
        await resolve_agent_credential(spec, store)

    get.assert_awaited_once_with(secret_path)
    error = caught.value
    assert error.credential_name == secret_path
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert error.cause is None
    assert "not found" in str(error)

    failure = Failure()
    DefaultFailureConverter().to_failure(error, None, failure)
    assert not failure.HasField("cause")
    assert "localhost" not in failure.message
    assert "HTTPStatusError" not in failure.message
    assert "not found" in failure.message

    diagnostics = [
        call.args[0] % call.args[1:]
        for call in warning.call_args_list
        if len(call.args) > 1
    ]
    assert len(diagnostics) == 1
    assert "HTTPStatusError" in diagnostics[0]
    assert "500" in diagnostics[0]
    assert "synthetic-sensitive-value" not in diagnostics[0]
    assert "password=***" in diagnostics[0]
    assert not warning.call_args.kwargs.get("exc_info")
