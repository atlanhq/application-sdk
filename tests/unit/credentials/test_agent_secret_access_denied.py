"""Secret-store access denial: PLATFORM on hosted paths, USER on the SDR path.

On the agent (SDR) path the store identity is the customer's IAM role, so a
denial is the customer's to fix. The probe must keep the subtype (hash-labelled,
no raw key) so the agent resolver can route it.
"""

import json

import pytest

from application_sdk.credentials.agent import (
    AgentSecretAccessDeniedError,
    resolve_agent_credential,
)
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors import Audience
from application_sdk.infrastructure._single_key_probe import probe_single_keys
from application_sdk.infrastructure.secrets import (
    SecretStoreAccessDeniedError,
    SecretStoreError,
)
from application_sdk.testing.mocks import MockSecretStore

RAW_KEY = "example-ref-key-do-not-leak"

_RAW = {
    "agent-name": "example-agent",
    "agent-type": "new-app-framework",
    "auth-type": "basic",
    "key-type": "single-key",
    "host": "db.example.com",
    "basic.password": RAW_KEY,
}


class RaisingStore(MockSecretStore):
    def __init__(self, exc: SecretStoreError) -> None:
        super().__init__({})
        self._exc = exc

    async def get_optional(self, name: str) -> str | None:
        raise self._exc


async def test_probe_keeps_access_denied_subtype_hash_labelled() -> None:
    async def fetch(value: str, label: str) -> str | None:
        raise SecretStoreAccessDeniedError(
            f"denied for {value}", secret_name=value, retryable=True
        )

    with pytest.raises(SecretStoreAccessDeniedError) as exc:
        await probe_single_keys([RAW_KEY], fetch)

    assert RAW_KEY not in str(exc.value)
    assert "sha256:" in str(exc.value)
    assert exc.value.cause is None
    assert exc.value.effective_retryable is True


async def test_agent_single_key_access_denied_is_user_audience() -> None:
    store = RaisingStore(
        SecretStoreAccessDeniedError("denied", secret_name=RAW_KEY, retryable=True)
    )

    with pytest.raises(AgentSecretAccessDeniedError) as exc:
        await resolve_agent_credential(AgentCredentialSpec.model_validate(_RAW), store)

    err = exc.value
    assert type(err).audience is Audience.USER
    assert isinstance(err, SecretStoreAccessDeniedError)
    assert RAW_KEY not in str(err)
    assert err.cause is None
    details = err.to_failure_details()
    assert details.audience is Audience.USER
    assert details.suggested_action
    assert RAW_KEY not in json.dumps(details.model_dump(mode="json"))


async def test_agent_single_key_throttling_stays_platform() -> None:
    store = RaisingStore(SecretStoreError("throttled", secret_name=RAW_KEY))

    with pytest.raises(SecretStoreError) as exc:
        await resolve_agent_credential(AgentCredentialSpec.model_validate(_RAW), store)

    assert not isinstance(exc.value, SecretStoreAccessDeniedError)
    assert type(exc.value).audience is Audience.PLATFORM
