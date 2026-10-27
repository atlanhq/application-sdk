"""Single-key resolution must not probe values that are never secret names.

An mTLS or Kerberos agent config carries companion files as
``objectstore://<key>`` paths, resolved later from the deployment object
store. Probed as a secret name, the path is rejected by a store that forbids
``/`` in names (the Kubernetes secret store), and that rejection is not a
"not found" answer, so the whole resolution failed.

Connection settings under ``extra`` (a compiled URL, a database name) are
probed like any other field, as on SDK <= 3.49: a customer may store one as a
secret. A plain one the store rejects is kept by the legacy fallback (FND-4407).
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


def _with_extra(raw: dict, shape: str, field: str, value: str) -> dict:
    if shape == "flat":
        raw.update({f"extra.{field}": value, "extra.role": "ROLE_REF"})
    elif shape == "auth-prefixed":
        raw.update({f"basic.extra.{field}": value, "basic.extra.role": "ROLE_REF"})
    else:
        raw["extra"] = {field: value, "role": "ROLE_REF"}
    return raw


def _basic_single_key() -> dict:
    return {
        "agent-name": "example-agent",
        "auth-type": "basic",
        "key-type": "single-key",
        "basic.username": "USER_REF",
    }


@pytest.mark.parametrize("shape", ["flat", "auth-prefixed", "nested"])
@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("compiled_url", "sqlserver://db.example.com:1433/reporting?encrypt=true"),
        ("database", "reporting_db"),
    ],
)
async def test_rejected_connection_literal_is_kept(
    shape: str, field: str, literal: str
) -> None:
    """A plain connection setting the store rejects as a name keeps its value
    (legacy fallback) and does not stop resolution of the other fields."""

    class RejectingStore(MockSecretStore):
        async def get_optional(self, name: str) -> str | None:
            if name == literal:
                raise SecretStoreError("invalid resource name", secret_name=name)
            return await super().get_optional(name)

    store = RejectingStore({"USER_REF": "resolved-user", "ROLE_REF": "resolved-role"})
    raw = _with_extra(_basic_single_key(), shape, field, literal)

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(raw), store
    )

    rendered = json.dumps(resolved)
    assert literal in rendered
    assert "resolved-role" in rendered
    assert "resolved-user" in rendered


@pytest.mark.parametrize("shape", ["flat", "auth-prefixed", "nested"])
@pytest.mark.parametrize("field", ["compiled_url", "database"])
async def test_connection_setting_stored_as_secret_resolves(
    shape: str, field: str
) -> None:
    """A customer may keep a connection setting in their store and put the
    secret's name in the field; it resolves, as on SDK <= 3.49."""
    store = MockSecretStore(
        {
            "USER_REF": "resolved-user",
            "ROLE_REF": "resolved-role",
            "DB_SETTING_REF": "resolved-setting",
        }
    )
    raw = _with_extra(_basic_single_key(), shape, field, "DB_SETTING_REF")

    resolved = await resolve_agent_credential(
        AgentCredentialSpec.model_validate(raw), store
    )

    assert "DB_SETTING_REF" in store.get_get_optional_calls()
    rendered = json.dumps(resolved)
    assert "resolved-setting" in rendered
    assert "DB_SETTING_REF" not in rendered


async def test_probe_skips_file_refs_for_every_caller() -> None:
    fetched: list[str] = []

    async def fetch(value: str, label: str) -> str | None:
        fetched.append(value)
        return None

    await probe_single_keys([CERT_REF, f" {CERT_REF} ", "USER_REF"], fetch)

    assert fetched == ["USER_REF"]
