"""Missing object-store configs must be credential verdicts, not gate outages."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.execution._temporal.preflight_gate import (
    PREFLIGHT_FAILED_ERROR_TYPE,
    PreflightGateInput,
    _is_definitive_credential_absence,
    build_preflight_gate_activity,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import PreflightGateMode
from application_sdk.infrastructure._dapr.credential_vault import DaprCredentialVault
from application_sdk.infrastructure.credential_vault import CredentialVaultError
from application_sdk.testing.preflight import single_outcome
from tests.unit.infrastructure.test_credential_vault import _binding_500, _vault_sleep

_GATE = "application_sdk.execution._temporal.preflight_gate"


@pytest.mark.parametrize("mode", [PreflightGateMode.HARD, PreflightGateMode.SOFT])
async def test_exhausted_missing_config_ladder_applies_gate_mode(mode) -> None:
    """Replay direct resolution against an absent blob without tenant access."""
    client = MagicMock()
    client.close = AsyncMock()
    client.invoke_binding = AsyncMock(
        side_effect=_binding_500("error invoking output binding: BlobNotFound")
    )
    vault = DaprCredentialVault(
        client,
        upstream_binding_name="upstream-objectstore",
        secret_store_name="secretstore",
    )
    handler = DefaultHandler()
    handler.preflight_check = AsyncMock()
    gate = build_preflight_gate_activity(
        handler, app_name="example", mode=mode, budget_seconds=30
    )
    with (
        _vault_sleep() as sleep,
        patch("application_sdk.infrastructure.DaprCredentialVault", return_value=vault),
        patch("application_sdk.infrastructure.AsyncDaprClient", return_value=client),
        patch(f"{_GATE}._require_secret_store", return_value=MagicMock()),
        patch(f"{_GATE}.logger") as logger,
    ):
        envelope = PreflightGateInput(
            extraction_method="direct", credential_guid="example-guid"
        )
        if mode is PreflightGateMode.HARD:
            with pytest.raises(ApplicationError) as raised:
                await gate(envelope)
            assert raised.value.type == PREFLIGHT_FAILED_ERROR_TYPE
            assert raised.value.non_retryable is True
            primary = raised.value.details[0]
            assert primary.category.value == "NOT_FOUND"
            assert primary.code == "NOT_FOUND_CREDENTIAL"
        else:
            result = await gate(envelope)
            assert result.status.value == "not_ready"

    assert client.invoke_binding.await_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [2.0, 5.0, 10.0]
    client.close.assert_awaited_once()
    handler.preflight_check.assert_not_awaited()
    row = single_outcome(logger)
    assert row["outcome"] == (
        "blocked" if mode is PreflightGateMode.HARD else "would_block"
    )
    assert row["gate_classification"] == "source_unverifiable"


def test_transport_vault_error_is_not_definitive_absence() -> None:
    """The old umbrella error must still fail open, never become a config fact."""
    transport = CredentialVaultError("store unavailable", cause=ConnectionResetError())
    collapsed = CredentialNotFoundError("example-guid")
    collapsed.__cause__ = transport
    assert _is_definitive_credential_absence(collapsed) is False
