"""Credential dependencies must not be classified as source SQL failures."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from application_sdk.errors.leaves import ColdStartRaceError, DependencyUnavailableError
from application_sdk.infrastructure.credential_vault import CredentialVaultError
from application_sdk.templates.contracts.sql_metadata import ExtractionTaskInput
from application_sdk.templates.sql_app import SqlApp


@pytest.mark.parametrize(
    "error_class", [CredentialVaultError, DependencyUnavailableError]
)
async def test_prime_preserves_credential_dependency_failure(error_class):
    """An empty HTTP timeout beneath vault errors never becomes a SQL timeout."""
    app = SqlApp.__new__(SqlApp)
    app._app_name = "test-app"
    task_input = ExtractionTaskInput(
        workflow_id="wf-test",
        output_path="/tmp/test",
        output_prefix="/tmp",
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
    )
    timeout = httpx.ConnectTimeout("")
    sidecar_error = ColdStartRaceError(message="sidecar unavailable", cause=timeout)
    sidecar_error.__cause__ = timeout
    vault_error = error_class(
        message="credential store unavailable", cause=sidecar_error
    )
    vault_error.__cause__ = sidecar_error
    init_client = AsyncMock(side_effect=vault_error)

    with (
        patch.object(SqlApp, "_init_sql_client", new=init_client),
        pytest.raises(error_class) as raised,
    ):
        await app.prime_sql_auth(task_input)

    assert raised.value is vault_error
    assert raised.value.__cause__ is sidecar_error
    assert raised.value.effective_retryable is True
    if error_class is CredentialVaultError:
        assert raised.value.code == "DEPENDENCY_UNAVAILABLE_CREDENTIAL_VAULT"
    init_client.assert_awaited_once_with(task_input)
