"""Single-key resolution probes every non-literal value, then keeps it on a store error.

An mTLS or Kerberos agent config carries companion files as
``objectstore://<key>`` paths, and a connection config carries a compiled URL
or a database name. Nothing in the payload records that these are not secret
names, so they are probed like any other value. A store that rejects such a
value as a name (the Kubernetes secret store rejects ``/``) answers with a
non-transient store error; that is logged at WARNING and the value is kept,
exactly as SDK 3.49 behaved (FND-4360).
"""

import json
from unittest.mock import patch

import pytest

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure._single_key_probe import probe_single_keys
from application_sdk.infrastructure.secrets import (
    SecretStoreError,
    SecretStoreTransientError,
)
from application_sdk.testing.mocks import MockSecretStore

CERT_REF = "objectstore://certs/example/client-mtls.zip"
_LOGGER = "application_sdk.infrastructure._single_key_probe.logger"


def _logged(mock_logger) -> str:
    return " ".join(
        str(arg) for call in mock_logger.warning.call_args_list for arg in call.args
    )


class SlashRejectingStore(MockSecretStore):
    """Rejects a secret name containing ``/``, as the Kubernetes store does."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rejected: list[str] = []

    async def get_optional(self, name: str) -> str | None:
        if "/" in name:
            self.rejected.append(name)
            raise SecretStoreError("invalid resource name", secret_name=name)
        return await super().get_optional(name)


async def test_mtls_cert_file_ref_is_probed_and_kept_on_store_error() -> None:
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

    with patch(_LOGGER) as mock_logger:
        resolved = await resolve_agent_credential(
            AgentCredentialSpec.model_validate(raw), store
        )

    assert CERT_REF in store.rejected
    assert "KEY_PASSWORD_REF" in store.get_get_optional_calls()
    rendered = json.dumps(resolved)
    assert CERT_REF in rendered
    assert "resolved-key-password" in rendered
    logged = _logged(mock_logger)
    assert "sha256:" in logged
    assert CERT_REF not in logged
    assert "treating as non-secret" in logged


@pytest.mark.parametrize("shape", ["flat", "auth-prefixed", "nested"])
@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("compiled_url", "sqlserver://db.example.com:1433/reporting?encrypt=true"),
        ("database", "reporting_db"),
    ],
)
async def test_connection_literal_is_probed_and_kept_on_store_error(
    shape: str, field: str, literal: str
) -> None:
    """A connection setting is probed like any value; a store that rejects it
    as a name (non-transient) must not stop resolution."""

    class RejectingStore(MockSecretStore):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.rejected: list[str] = []

        async def get_optional(self, name: str) -> str | None:
            if name == literal:
                self.rejected.append(name)
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

    with patch(_LOGGER) as mock_logger:
        resolved = await resolve_agent_credential(
            AgentCredentialSpec.model_validate(raw), store
        )

    assert literal in store.rejected
    rendered = json.dumps(resolved)
    assert literal in rendered
    assert "resolved-role" in rendered
    assert "resolved-user" in rendered
    logged = _logged(mock_logger)
    assert "sha256:" in logged
    assert literal not in logged


async def test_transient_error_on_a_literal_still_fails_resolution() -> None:
    """The keep-the-value rule does not cover a transient answer: a throttled
    lookup of any probed field fails and retries."""

    class ThrottledStore(MockSecretStore):
        async def get_optional(self, name: str) -> str | None:
            if name == "reporting_db":
                raise SecretStoreTransientError("throttled", secret_name=name)
            return await super().get_optional(name)

    raw = {
        "agent-name": "example-agent",
        "auth-type": "basic",
        "key-type": "single-key",
        "extra.database": "reporting_db",
    }

    with pytest.raises(SecretStoreTransientError) as exc_info:
        await resolve_agent_credential(
            AgentCredentialSpec.model_validate(raw), ThrottledStore({})
        )

    assert "reporting_db" not in str(exc_info.value)


async def test_probe_does_not_skip_file_refs() -> None:
    fetched: list[str] = []

    async def fetch(value: str, label: str) -> str | None:
        fetched.append(value)
        return None

    await probe_single_keys([CERT_REF, "USER_REF"], fetch)

    assert fetched == [CERT_REF, "USER_REF"]
