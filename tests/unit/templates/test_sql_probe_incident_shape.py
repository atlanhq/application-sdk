"""Replay the unclassified connect failure without claiming it becomes retryable."""

from dataclasses import dataclass
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from application_sdk.errors.leaves import InternalError
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    PrimeAuthOutput,
)
from application_sdk.templates.sql_app import SqlApp


@dataclass(kw_only=True)
class MssqlUnclassifiedError(InternalError):
    """Leaf contract read from atlan-mssql-app app/failures.py at v1.2.4."""

    code: ClassVar[str] = "INTERNAL_MSSQL_UNCLASSIFIED"


class Error(Exception):
    """ODBC exception shape, not a replacement installed driver package."""


def _connect_failure() -> MssqlUnclassifiedError:
    """Match the connect fallback and driver args in a production failure."""
    driver = Error(
        "HY000",
        "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Resource ID : 1. "
        "The request limit for the database is 8000 and has been reached. (10928)",
    )
    # app/clients.py:_raise_friendly_error at v1.2.4 emits this leaf, not busy.
    return MssqlUnclassifiedError(
        message="Unclassified MSSQL connection error",
        component="mssql_connect",
        classification_pending=True,
        cause=driver,
        suggested_action=(
            "Report this failure with the workflow run ID so the unhandled "
            "MSSQL connection error can be classified in classify_pyodbc_error."
        ),
    )


def test_preserves_actual_unclassified_connect_verdict():
    """The SDK must not discard even a connector's non-retryable verdict."""
    typed = _connect_failure()

    classified = SqlApp._classify_probe_exception(typed)

    assert classified is typed
    assert classified.code == "INTERNAL_MSSQL_UNCLASSIFIED"
    assert classified.effective_retryable is False
    assert classified.classification_pending is True


async def test_prime_preserves_actual_connect_failure_on_wire():
    """Replay prime with the original 10928 shape, including its mapping gap."""
    client = AsyncMock()
    client.get_results.side_effect = _connect_failure()
    app = SqlApp.__new__(SqlApp)
    app._app_name = "test-app"
    input = ExtractionTaskInput(
        workflow_id="wf-test",
        output_path="/tmp/test",
        output_prefix="/tmp",
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
    )
    with patch.object(SqlApp, "_init_sql_client", new=AsyncMock(return_value=client)):
        result = await app.prime_sql_auth(input)

    wire = PrimeAuthOutput.model_validate_json(result.model_dump_json())
    assert wire.success is False
    assert wire.failure is not None
    assert wire.failure.code == "INTERNAL_MSSQL_UNCLASSIFIED"
    assert wire.failure.retryable is False
    assert wire.error_type == "Error"
    assert "8000" in wire.error_message
    assert "(10928)" in wire.error_message
    client.close.assert_awaited_once()
