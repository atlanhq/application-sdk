"""Single-key credential resolution must not probe connection literals."""

import json

import pytest

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure.secrets import SecretStoreError
from application_sdk.testing.mocks import MockSecretStore


@pytest.mark.parametrize("shape", ["flat", "nested"])
@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("compiled_url", "server:1433/reporting?Encrypt=yes"),
        ("database", "reporting_database"),
    ],
)
async def test_connection_literal_never_probed(
    shape: str, field: str, literal: str
) -> None:
    """Rejecting a connection setting as a secret name must not block auth."""

    class RejectingStore(MockSecretStore):
        async def get_optional(self, name: str) -> str | None:
            if name == literal:
                raise SecretStoreError("Invalid secret name", secret_name=name)
            return await super().get_optional(name)

    store = RejectingStore(
        {"USER_REF": "resolved-user", "PWD_REF": "resolved-password"}
    )
    extra = {field: literal, "role": "ROLE_REF"}
    store.set("ROLE_REF", "resolved-role")
    raw = {
        "key-type": "single-key",
        "auth-type": "basic",
        "basic.username": "USER_REF",
        "basic.password": "PWD_REF",
    }
    raw.update(
        {f"extra.{k}": v for k, v in extra.items()}
        if shape == "flat"
        else {"extra": extra}
    )
    spec = AgentCredentialSpec.model_validate(raw)

    resolved = await resolve_agent_credential(spec, store)

    assert resolved["username"] == "resolved-user"
    assert resolved["password"] == "resolved-password"
    assert resolved["extra"] == {field: literal, "role": "resolved-role"}
    assert set(store.get_get_optional_calls()) == {"USER_REF", "PWD_REF", "ROLE_REF"}


@pytest.mark.parametrize("shape", ["flat", "nested"])
async def test_multikey_connection_fields_still_resolve(shape: str) -> None:
    """Candidate filtering must not alter the existing bundle contract."""
    extra = {"database": "DB_REF", "compiled_url": "URL_REF"}
    raw = {"secret-path": "bundle", "auth-type": "basic"}
    raw.update(
        {f"extra.{k}": v for k, v in extra.items()}
        if shape == "flat"
        else {"extra": extra}
    )
    store = MockSecretStore(
        {"bundle": json.dumps({"DB_REF": "resolved-db", "URL_REF": "resolved-url"})}
    )

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(raw), store
    )

    assert resolved["extra"] == {
        "database": "resolved-db",
        "compiled_url": "resolved-url",
    }
    assert store.get_get_calls() == ["bundle"]
    assert store.get_get_optional_calls() == []
