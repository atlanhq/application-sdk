"""Literal compiled URLs must not be interpreted as secret references."""

from __future__ import annotations

import json

import pytest

from application_sdk.credentials.agent import (
    check_secret_store_access,
    resolve_agent_credential,
)
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure.secrets import SecretStoreError
from application_sdk.testing.mocks import MockSecretStore

COMPILED_URL = "db.example.test:1433/catalog?Encrypt=yes&TrustServerCertificate=yes"


class RejectLiteralStore(MockSecretStore):
    """Model a store rejecting the literal address as an invalid secret name."""

    async def get_optional(self, name: str) -> str | None:
        if name == COMPILED_URL:
            raise SecretStoreError("invalid secret name", retryable=False)
        return await super().get_optional(name)


def _spec(shape: str, *, single_key: bool = True) -> AgentCredentialSpec:
    payload = {
        "agent-name": "test-agent",
        "auth-type": "basic",
        "basic.username": "USER_REF",
        "basic.password": "PASSWORD_REF",
    }
    payload.update(
        {"key-type": "single-key"} if single_key else {"secret-path": "bundle"}
    )
    if shape == "flat":
        payload["extra.compiled_url"] = COMPILED_URL
        payload["extra.database"] = "DATABASE_REF"
    else:
        payload["extra"] = {
            "compiled_url": COMPILED_URL,
            "database": "DATABASE_REF",
        }
    return AgentCredentialSpec.model_validate(payload)


def _store() -> RejectLiteralStore:
    store = RejectLiteralStore()
    store.set("USER_REF", "resolved-user")
    store.set("PASSWORD_REF", "resolved-password")
    store.set("DATABASE_REF", "resolved-database")
    return store


@pytest.mark.parametrize("shape", ["flat", "nested"])
async def test_single_key_resolution_skips_compiled_url(shape: str) -> None:
    store = _store()
    resolved = await resolve_agent_credential(_spec(shape), store)
    assert resolved["username"] == "resolved-user"
    assert resolved["password"] == "resolved-password"
    assert resolved["extra"]["compiled_url"] == COMPILED_URL
    assert resolved["extra"]["database"] == "resolved-database"
    assert set(store.get_get_optional_calls()) == {
        "USER_REF",
        "PASSWORD_REF",
        "DATABASE_REF",
    }


@pytest.mark.parametrize("shape", ["flat", "nested"])
async def test_preflight_skips_compiled_url(shape: str) -> None:
    store = _store()
    outcome = await check_secret_store_access(_spec(shape), store)
    assert outcome.passed
    assert not outcome.fatal
    assert not outcome.store_down
    assert outcome.substituted == 3
    assert outcome.resolved is not None
    assert outcome.resolved["extra"]["compiled_url"] == COMPILED_URL
    assert COMPILED_URL not in store.get_get_optional_calls()


@pytest.mark.parametrize("shape", ["flat", "nested"])
async def test_genuine_reference_errors_remain_fail_closed(shape: str) -> None:
    class DeniedStore(RejectLiteralStore):
        async def get_optional(self, name: str) -> str | None:
            if name == "PASSWORD_REF":
                raise SecretStoreError("access denied", retryable=False)
            return await super().get_optional(name)

    with pytest.raises(SecretStoreError):
        await resolve_agent_credential(_spec(shape), DeniedStore())


@pytest.mark.parametrize("shape", ["flat", "nested"])
async def test_bundle_cannot_substitute_literal_compiled_url(shape: str) -> None:
    store = MockSecretStore()
    store.set("bundle", json.dumps({COMPILED_URL: "not-a-url", "USER_REF": "user"}))
    resolved = await resolve_agent_credential(_spec(shape, single_key=False), store)
    assert resolved["username"] == "user"
    assert resolved["extra"]["compiled_url"] == COMPILED_URL
