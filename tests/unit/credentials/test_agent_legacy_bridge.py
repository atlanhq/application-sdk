"""The deprecated legacy single-key bridge (FND-4407).

Legacy ``agent_json`` payloads can't say which fields are secret names, so
single-key resolution probes every value. Since FND-4077, a store answer other
than "not found" failed the run, and that broke unchanged configs whose
*plain* values the store rejects as names. The bridge keeps the value (with a
WARNING) for legacy agent payloads only, and every legacy payload logs a
deprecation notice. Each test below replays a failure class from the
2026-10-20 → 10-27 fleet census.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from application_sdk.credentials.agent import (
    check_secret_store_access,
    resolve_agent_credential,
)
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure.secrets import (
    SecretStoreError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)


@pytest.fixture(autouse=True)
def _no_cold_start_wait(monkeypatch):
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        0.0,
    )


class AnsweringStore:
    """Holds *secrets*; answers every other name with a store error (not
    "not found"), the way each census backend answered a plain value."""

    def __init__(
        self, answer: SecretStoreError, secrets: dict[str, str] | None = None
    ) -> None:
        self.answer = answer
        self.secrets = secrets or {}

    async def get_optional(self, name: str) -> str | None:
        if name in self.secrets:
            return self.secrets[name]
        raise self.answer


def _legacy(**fields: object) -> AgentCredentialSpec:
    return AgentCredentialSpec.model_validate(
        {
            "agent-name": "sdr-agent",
            "key-type": "single-key",
            "auth-type": "basic",
            **fields,
        }
    )


#: (census class, store answer, plain-value fields that failed the run)
CENSUS_CLASSES = [
    pytest.param(
        SecretStoreError(
            "AccessDeniedException: no identity-based policy allows "
            "secretsmanager:GetSecretValue",
            retryable=True,
        ),
        {"basic.username": "svc_reader", "extra.databaseTerm": "SCHEMA"},
        id="aws-scoped-identity-access-denied",
    ),
    pytest.param(
        SecretStoreError("BadParameter: invalid secret name", retryable=True),
        {
            "extra.compiled_url": "mssql+pyodbc://db.example.com:1433/sales",
            "extra.database": "sales_db.prod",
        },
        id="azure-key-vault-invalid-name",
    ),
    pytest.param(
        SecretStoreError("invalid resource name: may not contain '/'", retryable=True),
        {"role.extra.aws_role_arn": "arn:aws:iam::123456789012:role/atlan"},
        id="kubernetes-invalid-name",
    ),
    pytest.param(
        SecretStoreError("ERR_PERMISSION_DENIED", retryable=False),
        {"basic.username": "svc_reader"},
        id="dapr-scoping-403",
    ),
]


class TestCensusClassesKeepTheirValues:
    @pytest.mark.parametrize("answer,fields", CENSUS_CLASSES)
    async def test_plain_values_survive_a_rejecting_store(
        self, answer: SecretStoreError, fields: dict[str, str]
    ) -> None:
        store = AnsweringStore(answer, {"prod/mysql-password": "s3cret"})
        spec = _legacy(**fields, **{"basic.password": "prod/mysql-password"})

        resolved = await resolve_agent_credential(spec, store)  # type: ignore[arg-type]

        assert resolved["password"] == "s3cret"
        flat = json.dumps(resolved)
        for value in fields.values():
            assert json.dumps(value) in flat

    @pytest.mark.parametrize("answer,fields", CENSUS_CLASSES)
    async def test_preflight_row_is_not_fatal(
        self, answer: SecretStoreError, fields: dict[str, str]
    ) -> None:
        result = await check_secret_store_access(
            _legacy(**fields),
            AnsweringStore(answer),  # type: ignore[arg-type]
        )
        assert not result.fatal
        assert not result.store_down
        assert result.resolved is not None


class TestTransientStillFails:
    """FND-4077's transient case stays protected under the bridge: a
    recognised throttle retries and never keeps the value."""

    async def test_throttled_probe_raises_and_keeps_nothing(self) -> None:
        from application_sdk.infrastructure.secrets import SecretStoreTransientError

        store = AnsweringStore(
            SecretStoreTransientError(
                "ThrottlingException: Rate exceeded", retryable=True
            )
        )
        with pytest.raises(SecretStoreError) as exc_info:
            await resolve_agent_credential(
                _legacy(**{"basic.password": "prod/mysql-password"}),
                store,  # type: ignore[arg-type]
            )
        assert exc_info.value.effective_retryable is True
        assert "prod/mysql-password" not in str(exc_info.value)


class TestOutagesStillFail:
    async def test_unreachable_store_fails_the_run(self) -> None:
        class DownStore:
            async def get_optional(self, name: str) -> str | None:
                raise SecretStoreUnavailableError(name)

        with pytest.raises((SecretStoreUnavailableError, SecretStoreUnreachableError)):
            await resolve_agent_credential(
                _legacy(**{"basic.username": "svc_reader"}),
                DownStore(),  # type: ignore[arg-type]
            )


class TestDeprecationNotice:
    async def test_legacy_single_key_logs_the_deprecation(self) -> None:
        store = AnsweringStore(SecretStoreError("denied"))
        with patch("application_sdk.credentials.agent.logger") as mock_logger:
            await resolve_agent_credential(
                _legacy(**{"basic.username": "svc_reader"}),
                store,  # type: ignore[arg-type]
            )
        messages = [str(c.args[0]) for c in mock_logger.warning.call_args_list]
        assert any("DEPRECATED agent_json format" in m for m in messages)

    async def test_legacy_multi_key_logs_the_deprecation(self) -> None:
        class BundleStore:
            async def get(self, name: str) -> str:
                return json.dumps({"user_ref": "svc_reader"})

        spec = AgentCredentialSpec.model_validate(
            {
                "agent-name": "sdr-agent",
                "secret-path": "prod/bundle",
                "basic.username": "user_ref",
            }
        )
        with patch("application_sdk.credentials.agent.logger") as mock_logger:
            await resolve_agent_credential(spec, BundleStore())  # type: ignore[arg-type]
        messages = [str(c.args[0]) for c in mock_logger.warning.call_args_list]
        assert any("DEPRECATED agent_json format" in m for m in messages)

    async def test_declared_payload_logs_no_deprecation(self) -> None:
        spec = AgentCredentialSpec.model_validate(
            {
                "format": 2,
                "agent-name": "sdr-agent",
                "basic.username": {"value": "svc_reader"},
            }
        )
        with patch("application_sdk.credentials.agent.logger") as mock_logger:
            await resolve_agent_credential(spec, AnsweringStore(SecretStoreError("x")))  # type: ignore[arg-type]
        assert mock_logger.warning.call_count == 0

    async def test_fallback_warning_names_the_hash_not_the_value(self) -> None:
        store = AnsweringStore(SecretStoreError("denied"))
        with patch(
            "application_sdk.infrastructure._single_key_probe.logger"
        ) as mock_logger:
            await resolve_agent_credential(
                _legacy(**{"basic.username": "svc_reader_plain"}),
                store,  # type: ignore[arg-type]
            )
        logged = " ".join(
            str(a) for c in mock_logger.warning.call_args_list for a in c.args
        )
        assert "DEPRECATED single-key fallback" in logged
        assert "svc_reader_plain" not in logged
        assert "sha256:" in logged
