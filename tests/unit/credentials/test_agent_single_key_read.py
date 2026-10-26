"""Legacy single-key: a non-outage store error is USER-facing and actionable."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from application_sdk.credentials.agent import resolve_agent_json
from application_sdk.errors import Audience, FailureCategory
from application_sdk.infrastructure.secrets import (
    AgentSingleKeyReadError,
    SecretStoreError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)
from application_sdk.testing.mocks import MockSecretStore

_RAW = "example-probe-value"
_LABEL = f"sha256:{hashlib.sha256(_RAW.encode()).hexdigest()[:8]}"


@pytest.fixture(autouse=True)
def _no_retry_window(monkeypatch):
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        0.0,
    )


def _payload(**extra: Any) -> str:
    return json.dumps(
        {
            "agent-name": "example-agent",
            "auth-type": "basic",
            "basic.password": _RAW,
            **extra,
        }
    )


def _single_key() -> str:
    return _payload(**{"key-type": "single-key"})


class _Store:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def get_optional(self, name: str) -> str | None:
        raise self._exc

    async def get(self, name: str) -> str:
        raise self._exc


class TestSingleKeyNonOutage:
    async def test_becomes_user_leaf_with_hashed_label_and_action(self) -> None:
        source = SecretStoreError(
            f"denied for {_RAW}", secret_name=_RAW, retryable=True
        )
        with pytest.raises(AgentSingleKeyReadError) as info:
            await resolve_agent_json(_single_key(), _Store(source))  # type: ignore[arg-type]
        err = info.value
        assert isinstance(err, SecretStoreError)
        assert err.category is FailureCategory.PRECONDITION
        assert err.audience is Audience.USER
        assert err.code.startswith("PRECONDITION_")
        assert err.effective_retryable is True  # kept from the probe error
        assert err.__cause__ is None
        assert _LABEL in str(err)
        assert _RAW not in str(err) and _RAW not in err.message
        assert err.suggested_action is not None
        assert "SDR's secret-store identity" in err.suggested_action
        assert _RAW not in err.suggested_action

    async def test_non_retryable_stays_non_retryable(self) -> None:
        source = SecretStoreError("denied", retryable=False)
        with pytest.raises(AgentSingleKeyReadError) as info:
            await resolve_agent_json(_single_key(), _Store(source))  # type: ignore[arg-type]
        assert info.value.effective_retryable is False


class TestOutageUnchanged:
    async def test_unavailable_stays_unavailable(self) -> None:
        source = SecretStoreUnavailableError(_RAW)
        with pytest.raises(SecretStoreUnavailableError) as info:
            await resolve_agent_json(_single_key(), _Store(source))  # type: ignore[arg-type]
        assert not isinstance(info.value, AgentSingleKeyReadError)

    async def test_unreachable_stays_unreachable(self) -> None:
        source = SecretStoreUnreachableError(_RAW, component="c", attempts=2)
        with pytest.raises(SecretStoreUnreachableError) as info:
            await resolve_agent_json(_single_key(), _Store(source))  # type: ignore[arg-type]
        assert not isinstance(info.value, AgentSingleKeyReadError)


class TestMultiKeyUnchanged:
    async def test_secret_path_branch_still_raises_plain_store_error(self) -> None:
        source = SecretStoreError("denied", retryable=False)
        payload = _payload(**{"secret-path": "bundle/example"})
        with pytest.raises(SecretStoreError) as info:
            await resolve_agent_json(payload, _Store(source))  # type: ignore[arg-type]
        assert not isinstance(info.value, AgentSingleKeyReadError)

    async def test_single_key_success_is_unchanged(self) -> None:
        store = MockSecretStore({_RAW: "resolved-value"})
        out = await resolve_agent_json(_single_key(), store)
        assert out["password"] == "resolved-value"
