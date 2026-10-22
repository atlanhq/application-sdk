"""Single-key probes must not interpret compiled connection URLs as secret keys."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure.secrets import SecretStoreError
from application_sdk.testing.mocks import MockSecretStore


@pytest.mark.parametrize("shape", ["dotted", "nested"])
async def test_compiled_url_is_not_probed_as_a_secret(shape: str) -> None:
    """Replay the agent payload shape with a store rejecting URL-shaped keys.

    The production RCA identified extra.compiled_url as the failed probe.
    Both the current dotted form and the legacy nested form carry literals.
    """
    compiled_url = "mssql+pyodbc://db.example:1433/catalog?driver=ODBC&encrypt=yes"
    extra: dict[str, Any] = {
        "compiled_url": compiled_url,
        "database": "DATABASE_REF",
    }
    fields = (
        {f"extra.{key}": value for key, value in extra.items()}
        if shape == "dotted"
        else {"extra": extra}
    )
    spec = AgentCredentialSpec.model_validate(
        {
            "agent-name": "test-agent",
            "key-type": "single-key",
            "auth-type": "basic",
            "basic.username": "USERNAME_REF",
            "basic.password": "PASSWORD_REF",
            **fields,
        }
    )
    store = MockSecretStore()
    values = {
        "USERNAME_REF": "test-user",
        "PASSWORD_REF": "test-password",
        "DATABASE_REF": "catalog",
    }

    async def get_optional(name: str) -> str | None:
        if name == compiled_url:
            raise SecretStoreError("Invalid secret name", retryable=True)
        return values.get(name)

    store.get_optional = AsyncMock(side_effect=get_optional)

    resolved = await resolve_agent_credential(spec, store)

    assert resolved["username"] == "test-user"
    assert resolved["password"] == "test-password"
    assert resolved["extra"] == {"compiled_url": compiled_url, "database": "catalog"}
    assert store.get_optional.await_count == 3
    assert {call.args[0] for call in store.get_optional.await_args_list} == set(values)


async def test_real_secret_store_failure_remains_fatal() -> None:
    """Skipping literal URLs must not mask an outage on a credential ref."""
    store = MockSecretStore()
    store.get_optional = AsyncMock(
        side_effect=SecretStoreError("Secret store unavailable", retryable=True)
    )
    spec = AgentCredentialSpec.model_validate(
        {
            "agent-name": "test-agent",
            "key-type": "single-key",
            "basic.password": "PASSWORD_REF",
        }
    )

    with pytest.raises(SecretStoreError) as failure:
        await resolve_agent_credential(spec, store)

    assert failure.value.effective_retryable is True
    store.get_optional.assert_awaited_once_with("PASSWORD_REF")
