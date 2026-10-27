"""Telemetry for legacy single-key probing (FND-4407).

Each legacy single-key resolution logs one WARNING record with the outcome of
every probed field, so we can measure which workflows still use the legacy
format and what each field really is. That per-field evidence is what
migrating a published workflow to declared sources needs:

- resolved: a real secret ref, ``{"valueFrom": {"secretKeyRef": ...}}``;
- absent / refused: a plain value, ``{"value": ...}``.

The record must reach Atlan from an SDR. Only allowlisted ``atlan.*`` extras
survive the object-store export, and only as scalars. It must never carry a
value or a ref name, only field names.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.infrastructure.secrets import (
    SecretStoreError,
    SecretStoreTransientError,
    SecretStoreUnavailableError,
)


@pytest.fixture(autouse=True)
def _no_cold_start_wait(monkeypatch):
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        0.0,
    )


PLAIN_USERNAME = "svc_reader_plain"
SECRET_REF = "prod/db-password-ref"
PLAIN_DATABASE = "sales_db.prod"


class MixedStore:
    """Resolves the one ref, has no username, and refuses the database name."""

    def __init__(self, database_answer: Exception) -> None:
        self.database_answer = database_answer

    async def get_optional(self, name: str) -> str | None:
        if name == SECRET_REF:
            return "s3cret"
        if name == PLAIN_DATABASE:
            raise self.database_answer
        return None


def _spec() -> AgentCredentialSpec:
    return AgentCredentialSpec.model_validate(
        {
            "agent-name": "sdr-agent",
            "secret-manager": "awssecretmanager",
            "key-type": "single-key",
            "auth-type": "basic",
            "host": "db.example.com",
            "basic.username": PLAIN_USERNAME,
            "basic.password": SECRET_REF,
            "extra.databaseTerm": PLAIN_DATABASE,
        }
    )


def _telemetry_record(mock_logger) -> dict[str, Any]:
    records = [
        c
        for c in mock_logger.warning.call_args_list
        if "legacy single-key probe outcomes" in str(c.args[0])
    ]
    assert len(records) == 1, "exactly one telemetry record per resolution"
    return records[0].kwargs["extra"]


async def _resolve(store) -> tuple[dict[str, Any], BaseException | None]:
    with patch("application_sdk.credentials.agent.logger") as mock_logger:
        try:
            await resolve_agent_credential(_spec(), store)  # type: ignore[arg-type]
            raised = None
        except SecretStoreError as exc:
            raised = exc
    return _telemetry_record(mock_logger), raised


class TestOutcomesPerField:
    async def test_refused_field_is_recorded_even_when_the_run_fails(self) -> None:
        extra, _ = await _resolve(
            MixedStore(SecretStoreError("AccessDeniedException", retryable=True))
        )
        outcomes = dict(
            pair.split("=")
            for pair in extra["atlan.agent_json.probe.outcomes"].split(",")
        )
        assert outcomes["basic.password"] == "resolved"
        assert outcomes["basic.username"] == "absent"
        assert outcomes["extra.databaseTerm"] in {"refused", "refused_kept"}
        assert extra["atlan.agent_json.probe.resolved"] == 1
        assert extra["atlan.agent_json.probe.absent"] == 1
        assert extra["atlan.agent_json.format"] == "legacy-single-key"
        assert extra["atlan.agent_json.secret_manager"] == "awssecretmanager"

    @pytest.mark.parametrize(
        "answer,outcome",
        [
            (SecretStoreTransientError("Rate exceeded", retryable=True), "transient"),
            (SecretStoreUnavailableError("x"), "outage"),
        ],
    )
    async def test_transient_and_outage_are_distinguished(
        self, answer: Exception, outcome: str
    ) -> None:
        extra, raised = await _resolve(MixedStore(answer))
        assert raised is not None
        assert (
            f"extra.databaseTerm={outcome}" in extra["atlan.agent_json.probe.outcomes"]
        )


class TestExportSafety:
    async def test_extras_are_exportable_scalars_with_the_atlan_prefix(self) -> None:
        extra, _ = await _resolve(MixedStore(SecretStoreError("denied")))
        assert extra
        for key, value in extra.items():
            assert key.startswith("atlan."), key
            assert isinstance(value, (str, int)), (key, type(value))

    async def test_extras_survive_the_object_store_export_filter(self) -> None:
        """The NDJSON sink an SDR uploads to Atlan keeps only allowlisted
        extras; run the record's extras through that same filter."""
        from application_sdk.observability.logger_adaptor import _build_extra_dict

        extra, _ = await _resolve(MixedStore(SecretStoreError("denied")))
        assert _build_extra_dict(dict(extra)) == extra

    async def test_never_carries_values_or_ref_names(self) -> None:
        extra, _ = await _resolve(MixedStore(SecretStoreError("denied")))
        flat = repr(extra)
        for secret_material in (PLAIN_USERNAME, SECRET_REF, PLAIN_DATABASE, "s3cret"):
            assert secret_material not in flat


async def test_declared_payload_logs_no_legacy_telemetry() -> None:
    spec = AgentCredentialSpec.model_validate(
        {
            "format": 2,
            "agent-name": "sdr-agent",
            "basic.username": {"value": PLAIN_USERNAME},
        }
    )
    with patch("application_sdk.credentials.agent.logger") as mock_logger:
        await resolve_agent_credential(spec, MixedStore(SecretStoreError("x")))  # type: ignore[arg-type]
    assert not any(
        "legacy single-key probe outcomes" in str(c.args[0])
        for c in mock_logger.warning.call_args_list
    )
