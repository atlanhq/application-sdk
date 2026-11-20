"""Single-key resolution must not probe values that are never secret names.

An mTLS or Kerberos agent config carries companion files as
``objectstore://<key>`` paths, resolved later from the deployment object
store. Probed as a secret name, the path is rejected by a store that forbids
``/`` in names (the Kubernetes secret store), and that rejection is not a
"not found" answer, so the whole resolution failed. A compiled connection
URL under ``extra`` is a literal in the same way. A database name under
``extra`` is not: legacy single-key payloads may carry a secret name there.
"""

import json

import pytest

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure._single_key_probe import probe_single_keys
from application_sdk.infrastructure.secrets import SecretStoreError
from application_sdk.testing.mocks import MockSecretStore

CERT_REF = "objectstore://certs/example/client-mtls.zip"


class SlashRejectingStore(MockSecretStore):
    """Rejects a secret name containing ``/``, as the Kubernetes store does."""

    async def get_optional(self, name: str) -> str | None:
        if "/" in name:
            raise SecretStoreError("invalid resource name", secret_name=name)
        return await super().get_optional(name)


async def test_mtls_cert_file_ref_is_not_probed() -> None:
    store = SlashRejectingStore({"KEY_PASSWORD_REF": "resolved-key-password"})
    raw = {
        "agent-name": "example-agent",
        "agent-type": "new-app-framework",
        "auth-type": "mtls",
        "key-type": "single-key",
        "host": "broker.example.com:9094",
        "mtls.extra.mtls_cert": CERT_REF,
        "mtls.extra.key_password": "KEY_PASSWORD_REF",
    }

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(raw), store
    )

    assert CERT_REF not in store.get_get_optional_calls()
    assert "KEY_PASSWORD_REF" in store.get_get_optional_calls()
    rendered = json.dumps(resolved)
    assert CERT_REF in rendered
    assert "resolved-key-password" in rendered


@pytest.mark.parametrize("shape", ["flat", "auth-prefixed", "nested"])
@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("compiled_url", "sqlserver://db.example.com:1433/reporting?encrypt=true"),
    ],
)
async def test_connection_literal_is_not_probed(
    shape: str, field: str, literal: str
) -> None:
    """A connection setting is a literal, not a secret name: a store that
    rejects it as a name must not stop resolution."""

    class RejectingStore(MockSecretStore):
        async def get_optional(self, name: str) -> str | None:
            if name == literal:
                raise SecretStoreError("invalid resource name", secret_name=name)
            return await super().get_optional(name)

    store = RejectingStore({"USER_REF": "resolved-user", "ROLE_REF": "resolved-role"})
    raw: dict = {
        "agent-name": "example-agent",
        "auth-type": "basic",
        "key-type": "single-key",
        "basic.username": "USER_REF",
    }
    if shape == "flat":
        raw.update({f"extra.{field}": literal, "extra.role": "ROLE_REF"})
    elif shape == "auth-prefixed":
        raw.update({f"basic.extra.{field}": literal, "basic.extra.role": "ROLE_REF"})
    else:
        raw["extra"] = {field: literal, "role": "ROLE_REF"}

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(raw), store
    )

    assert literal not in store.get_get_optional_calls()
    rendered = json.dumps(resolved)
    assert literal in rendered
    assert "resolved-role" in rendered
    assert "resolved-user" in rendered


async def test_probe_skips_file_refs_for_every_caller() -> None:
    fetched: list[str] = []

    async def fetch(value: str, label: str) -> str | None:
        fetched.append(value)
        return None

    await probe_single_keys([CERT_REF, f" {CERT_REF} ", "USER_REF"], fetch)

    assert fetched == ["USER_REF"]


def _legacy_raw(shape: str, database: str) -> dict:
    raw: dict = {
        "agent-name": "example-agent",
        "auth-type": "basic",
        "key-type": "single-key",
        "host": "db.example.com",
        "basic.username": "USER_REF",
        "basic.password": "PASS_REF",
    }
    if shape == "flat":
        raw["extra.database"] = database
    elif shape == "auth-prefixed":
        raw["basic.extra.database"] = database
    else:
        raw["extra"] = {"database": database}
    return raw


@pytest.mark.parametrize("shape", ["flat", "auth-prefixed", "nested"])
async def test_legacy_database_secret_name_is_resolved(shape: str) -> None:
    """A legacy single-key payload may name a secret in ``extra.database``
    (SDK <= 3.51 resolved it). Treating it as a literal sent the secret's
    name to the server as the database, which refused to open it."""
    store = MockSecretStore(
        {
            "USER_REF": "resolved-user",
            "PASS_REF": "resolved-pass",
            "DB_SECRET_NAME": "real_db",
        }
    )

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(_legacy_raw(shape, "DB_SECRET_NAME")),
        store,
    )

    assert "DB_SECRET_NAME" in store.get_get_optional_calls()
    rendered = json.dumps(resolved)
    assert "real_db" in rendered
    assert "DB_SECRET_NAME" not in rendered


@pytest.mark.parametrize("shape", ["flat", "auth-prefixed", "nested"])
async def test_legacy_literal_database_the_store_rejects_is_kept(shape: str) -> None:
    """A plain database name is probed like any legacy field; one the store
    rejects as a secret name is kept as entered (the legacy bridge), and
    resolution still succeeds."""

    probed: list[str] = []

    class RejectingStore(MockSecretStore):
        async def get_optional(self, name: str) -> str | None:
            probed.append(name)
            if name == "reporting_db":
                raise SecretStoreError("invalid resource name", secret_name=name)
            return await super().get_optional(name)

    store = RejectingStore({"USER_REF": "resolved-user", "PASS_REF": "resolved-pass"})

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(_legacy_raw(shape, "reporting_db")),
        store,
    )

    assert "reporting_db" in probed
    rendered = json.dumps(resolved)
    assert "reporting_db" in rendered
    assert "resolved-user" in rendered
    assert "resolved-pass" in rendered
