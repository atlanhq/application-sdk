"""Single-key resolution must not probe an ``objectstore://`` file reference.

An mTLS or Kerberos agent config carries companion files as
``objectstore://<key>`` paths, resolved later from the deployment object
store. Probed as a secret name, the path is rejected by a store that forbids
``/`` in names (the Kubernetes secret store), and that rejection is not a
"not found" answer, so the whole resolution failed.
"""

import json

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


async def test_probe_skips_file_refs_for_every_caller() -> None:
    fetched: list[str] = []

    async def fetch(value: str, label: str) -> str | None:
        fetched.append(value)
        return None

    await probe_single_keys([CERT_REF, "USER_REF"], fetch)

    assert fetched == ["USER_REF"]
